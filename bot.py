#!/usr/bin/env python3
import asyncio, json, logging, os, sqlite3, threading, time
from pathlib import Path
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from playwright.async_api import async_playwright
import httpx

TOKEN=os.environ["TELEGRAM_BOT_TOKEN"].strip()
ALLOWED={x.strip() for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS","").split(",") if x.strip()}
INTERVAL=max(60,int(os.getenv("RENEW_INTERVAL_MINUTES","60"))*60)
AUTO_RENEW=os.getenv("AUTO_RENEW","NO").upper()=="YES"
AUTO_PAY=os.getenv("AUTO_PAY","NO").upper()=="YES"
MAX_AMOUNT=float(os.getenv("MAX_RENEW_AMOUNT","0"))
PAYMENT_PROVIDER=os.getenv("PAYMENT_PROVIDER","balance")
LOGIN_USER=os.getenv("HOSTSLATE_USERNAME","")
LOGIN_PASSWORD=os.getenv("HOSTSLATE_PASSWORD","")
HOSTSLATE_API_KEY=os.getenv("HOSTSLATE_API_KEY","").strip()
BASE="https://www.hostslate.com"
API_BASE=BASE+"/api/v1"
TRAFFIC_ALERT_PERCENT=float(os.getenv("TRAFFIC_ALERT_PERCENT","80"))
RENEW_BILLING_CYCLE=os.getenv("RENEW_BILLING_CYCLE","monthly").strip().lower()
API_AUTH_PREFIX=os.getenv("HOSTSLATE_API_AUTH_PREFIX","Bearer ")
DATA=Path(os.getenv("DATA_DIR","/app/data")); PROFILE=DATA/"hostslate-profile"; DB=DATA/"state.db"
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
lock=threading.Lock(); state={"running":False,"paused":False,"busy":False,"phase":"空闲","current":"-","total":0,"done":0,"runs":0,"next_run":"-","last":"未执行"}
runtime_app=None
progress_messages={}
run_lock=asyncio.Lock()

def progress_text():
 progress=f"🔁 第 {state['runs']} 次执行" if state['runs'] else "⏳ 尚未执行"
 mode="⏸️ 已暂停" if state["paused"] else ("🟢 运行中" if state["running"] else "⚪ 空闲")
 return ("🔄 *HostSlate 续费任务*\n\n"
         f"{mode}　{'处理中' if state['busy'] else '待命'}\n"
         f"📍 阶段：{state['phase']}\n"
         f"🖥️ 当前：{state['current']}\n"
         f"📈 进度：{progress}\n"
         f"⏱️ 下次运行：{state['next_run']}\n"
         f"📝 最近结果：{state['last']}")

async def progress(text):
 state["phase"]=text
 logging.info("PROGRESS %s", text)
 if runtime_app:
  for uid in ALLOWED:
   try:
    chat_id=int(uid); body=progress_text(); mid=progress_messages.get(chat_id)
    if mid:
     try: await runtime_app.bot.edit_message_text(chat_id=chat_id,message_id=mid,text=body,parse_mode="Markdown")
     except Exception: progress_messages.pop(chat_id,None)
    if chat_id not in progress_messages:
     m=await runtime_app.bot.send_message(chat_id,body,parse_mode="Markdown")
     progress_messages[chat_id]=m.message_id
   except Exception: pass

def allowed(u): return not ALLOWED or str(u.effective_user.id) in ALLOWED
def initdb():
 DATA.mkdir(parents=True,exist_ok=True); sqlite3.connect(DB).execute("create table if not exists orders (key text primary key, result text, ts integer)").connection.commit()
async def msg(update,text):
 if allowed(update): await update.message.reply_text(text)
async def status(update,context):
 if not allowed(update): return
 await update.message.reply_text(progress_text(),parse_mode="Markdown")
async def start(update,context):
 if not allowed(update): return
 with lock: state.update(running=True,paused=False)
 await update.message.reply_text("🟢 *续费循环已启动*\n\n立即执行第一轮，之后按间隔自动执行。",parse_mode="Markdown")
 if not run_lock.locked():
  state["next_run"]="现在"
  state["last"]=await run_once(force=False)

async def pause(update,context):
 if not allowed(update): return
 with lock: state["paused"]=True
 await progress("已暂停，不会开始新的续费操作")
 await update.message.reply_text("⏸️ *任务已暂停*",parse_mode="Markdown")
async def resume(update,context):
 if not allowed(update): return
 with lock: state.update(running=True,paused=False)
 await progress("已恢复，等待下一轮任务")
 await update.message.reply_text("▶️ *任务已恢复*",parse_mode="Markdown")
async def stop(update,context):
 if not allowed(update): return
 with lock: state.update(running=False,paused=False)
 await progress("任务已停止")
 await update.message.reply_text("⏹️ *任务已停止*",parse_mode="Markdown")
async def run_once(force=False):
 if run_lock.locked(): return "已有任务正在执行，已跳过重复启动"
 async with run_lock:
  state["next_run"]="执行中"
  result=await renew_once(force=force)
  state["last"]=result
  await progress("本轮任务已完成")
  return result
async def check(update,context):
 if not allowed(update): return
 await update.message.reply_text("⏳ 正在强制执行一次续费检查……")
 was_running=state["running"]; state["running"]=True
 result=await run_once(force=True)
 state["running"]=was_running; await update.message.reply_text(result)
def pick(obj, names, default=None):
 if isinstance(obj, dict):
  for name in names:
   if name in obj and obj[name] is not None: return obj[name]
  for v in obj.values():
   r=pick(v,names,None)
   if r is not None: return r
 return default

def gib(v):
 try:
  n=float(v or 0)
  return n/1024**3 if n > 1024**2 else n
 except Exception: return 0.0

async def api_get(path):
 if not HOSTSLATE_API_KEY: raise RuntimeError("未设置 HOSTSLATE_API_KEY")
 async with httpx.AsyncClient(timeout=30) as c:
  r=await c.get(API_BASE+path,headers={"Authorization":API_AUTH_PREFIX+HOSTSLATE_API_KEY})
  if r.status_code in (401,403):
   raise RuntimeError(f"HostSlate API {r.status_code}：API Key 无效或没有对应权限（认证方式：{API_AUTH_PREFIX.strip() or '原始Key'}）")
  r.raise_for_status(); return r.json()

async def traffic_text():
 data=await api_get("/portal/instances")
 items=data.get("data",data) if isinstance(data,dict) else data
 if isinstance(items,dict): items=items.get("items",items.get("instances",[]))
 if not isinstance(items,list): items=[]
 lines=["📡 *HostSlate VPS 流量监控*","━━━━━━━━━━━━"]
 for i,item in enumerate(items):
  iid=item.get("id") if isinstance(item,dict) else None
  name=pick(item,["name","hostname","label"],f"实例 {i+1}")
  try: metrics=await api_get(f"/portal/instances/{iid}/metrics") if iid else {}
  except Exception as e: metrics={"error":str(e)}
  try: packs=await api_get(f"/portal/instances/{iid}/traffic-packages") if iid else {}
  except Exception: packs={}
  used=pick(metrics,["traffic_used","used_traffic","total_traffic","bytes_used"],None)
  if used is None: used=pick(item,["traffic_used","used_traffic","trafficUsage","bandwidth_used"],0)
  limit=pick(packs,["traffic_limit","total","quota","included"],None)
  if limit is None: limit=pick(item,["traffic_limit","traffic_quota","bandwidth"],0)
  down=pick(metrics,["download","download_bytes","rx_bytes"],0); up=pick(metrics,["upload","upload_bytes","tx_bytes"],0)
  used_g=gib(used); limit_g=gib(limit); pct=(used_g/limit_g*100) if limit_g else 0
  flag="⚠️" if pct >= TRAFFIC_ALERT_PERCENT else "✅"
  lines += [f"\n🖥️ *{name}*",f"📥 下载：{gib(down):.2f} GB",f"📤 上传：{gib(up):.2f} GB",f"📊 已用：{used_g:.2f} GB",f"📦 配额：{limit_g:.2f} GB",f"{flag} 使用率：{pct:.1f}%"]
 return "\n".join(lines) if len(lines)>2 else "📡 未找到实例流量数据。"

async def traffic(update,context):
 if not allowed(update): return
 try: await update.message.reply_text(await traffic_text(),parse_mode="Markdown")
 except Exception as e: await update.message.reply_text("获取流量失败："+str(e)[:300])

async def renew_once(force=False):
 state["runs"]+=1
 if HOSTSLATE_API_KEY:
  return await api_renew_once(force=force)
 state.update(busy=True, done=0, total=0, current="-", phase="准备启动")
 try:
  async with async_playwright() as p:
   await progress("启动浏览器")
   browser=await p.chromium.launch_persistent_context(str(PROFILE),headless=True)
   page=await browser.new_page(); await progress("打开登录页")
   await page.goto(BASE+"/login",wait_until="domcontentloaded")
   try: await page.wait_for_url("**/portal/**",timeout=15000)
   except Exception:
    state.update(busy=False,phase="等待登录或人机验证")
    return "登录未完成：请先完成 HostSlate 登录和人机验证。"
   await progress("读取实例列表")
   await page.goto(BASE+"/portal/instances",wait_until="networkidle")
   buttons=page.get_by_text("续费",exact=False); n=await buttons.count(); state["total"]=n
   if not n:
    await browser.close(); state.update(busy=False,phase="未发现续费入口")
    return "未发现续费按钮。"
   done=0
   for i in range(n):
    if state["paused"] or not state["running"]:
     state.update(busy=False,phase="已暂停/停止"); await browser.close(); return f"任务中止，已处理 {done}/{n}"
    state["current"]=f"第 {i+1}/{n} 个实例"; await progress(f"检查 {state['current']}")
    if not AUTO_RENEW: continue
    key=f"{await buttons.nth(i).inner_text()}-{i}"
    con=sqlite3.connect(DB); old=con.execute("select 1 from orders where key=?",(key,)).fetchone(); con.close()
    if old: state["done"]=i+1; continue
    await buttons.nth(i).click(); await page.wait_for_timeout(1000); await progress(f"已创建 {state['current']} 续费流程")
    body=(await page.locator("body").inner_text()).lower()
    if AUTO_PAY and MAX_AMOUNT == 0 and any(x in body for x in ["0.00", "￥0", "¥0", "免费"]):
     pay=page.get_by_text("余额支付",exact=False)
     if await pay.count():
      await progress(f"正在余额支付 {state['current']}"); await pay.first.click(); await page.wait_for_timeout(1500)
      result="renew-paid" if not await page.get_by_text("支付失败",exact=False).count() else "payment-failed"
     else: result="payment-control-not-found"
    else: result="renew-clicked-awaiting-confirmation"
    con=sqlite3.connect(DB); con.execute("insert or replace into orders values(?,?,?)",(key,result,int(time.time()))); con.commit(); con.close(); done+=1; state["done"]=i+1
    await progress(f"完成 {state['done']}/{n}：{result}")
   await browser.close(); state.update(busy=False,phase="本轮完成",current="-")
   return f"本轮完成：发现 {n} 个入口，处理 {done} 个，自动支付={AUTO_PAY}。"
 except Exception as e:
  state.update(busy=False,phase="执行失败"); return "执行失败："+str(e)[:500]
async def api_renew_once(force=False):
 state.update(busy=True, done=0, total=0, current="-", phase="API 获取实例")
 try:
  data=await api_get("/portal/instances"); items=data.get("data",data) if isinstance(data,dict) else data
  if isinstance(items,dict): items=items.get("items",items.get("instances",[]))
  if not isinstance(items,list): items=[]
  state["total"]=len(items); done=0
  for item in items:
   if state["paused"] or not state["running"]: break
   iid=item.get("id"); name=pick(item,["name","hostname","label"],str(iid)); state["current"]=str(name); state["done"]=done+1; await progress(f"API 检查 {name}")
   if not AUTO_RENEW: done+=1; state["done"]=done; continue
   period=pick(item,["billing_period","period","next_due_at"],"current")
   key=f"api-renew-{iid}-{period}"
   con=sqlite3.connect(DB); old=con.execute("select 1 from orders where key=?",(key,)).fetchone(); con.close()
   if old and not force: done+=1; state["done"]=done; await progress(f"跳过已处理订单：{name}"); continue
   await progress(f"创建续费订单：{name}")
   async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as c:
    h={"Authorization":API_AUTH_PREFIX+HOSTSLATE_API_KEY,"Content-Type":"application/json"}
    r=await c.post(f"{API_BASE}/portal/instances/{iid}/renew",headers=h,json={"billing_cycle":RENEW_BILLING_CYCLE})
    if r.status_code == 422:
     raise RuntimeError(f"续费参数校验失败：{r.text[:400]}")
    r.raise_for_status(); order=r.json()
    order=order.get("data",order); oid=pick(order,["id","order_id"])
    amount=float(pick(order,["amount","total_amount","payable_amount"],0) or 0)
    await progress(f"订单已创建：{name}，金额 {amount:.2f}")
    result="renew-created"
    if AUTO_PAY and amount <= MAX_AMOUNT and oid:
     await progress(f"余额支付 {name}（{amount:.2f}）")
     pr=await c.post(f"{API_BASE}/portal/orders/{oid}/pay",headers=h,json={"provider":"balance","method":"balance"})
     if pr.status_code >= 400: raise RuntimeError(f"余额支付失败 HTTP {pr.status_code}: {pr.text[:300]}")
     result="renew-paid"
    elif AUTO_PAY and amount > MAX_AMOUNT:
     await progress(f"金额 {amount:.2f} 超过上限 {MAX_AMOUNT:.2f}，跳过支付")
    else:
     await progress(f"订单待支付：{name}")
    con=sqlite3.connect(DB); con.execute("insert or replace into orders values(?,?,?)",(key,result,int(time.time()))); con.commit(); con.close()
   done+=1; state["done"]=done; await progress(f"完成本次实例：{name} · {result}")
  result_text=f"API 本轮完成，自动余额支付={AUTO_PAY}"
  state.update(busy=False,phase="本轮完成",current="-",last=result_text)
  await progress("本轮完成")
  return result_text
 except Exception as e:
  state.update(busy=False,phase="API 执行失败"); return "API 执行失败："+str(e)[:500]
async def loop(app):
 while True:
  if state["running"] and not state["paused"] and not run_lock.locked():
   state["next_run"]="现在"
   result=await run_once(force=False)
   for uid in ALLOWED:
    try: await app.bot.send_message(int(uid),result)
    except Exception: pass
   state["next_run"]=f"约 {INTERVAL//60} 分钟后"
  else:
   state["next_run"]=f"约 {INTERVAL//60} 分钟后" if state["running"] else "未启动"
  await asyncio.sleep(INTERVAL)
async def post_init(app):
 global runtime_app
 runtime_app=app
 await app.bot.set_my_commands([
  BotCommand("start_task", "启动循环续费"),
  BotCommand("pause_task", "暂停任务"),
  BotCommand("resume_task", "恢复任务"),
  BotCommand("stop_task", "停止任务"),
  BotCommand("status", "查看运行状态"),
  BotCommand("renew", "立即检查一次"),
  BotCommand("traffic", "查看 VPS 流量"),
 ])
 asyncio.create_task(loop(app))
def main():
 initdb(); app=Application.builder().token(TOKEN).post_init(post_init).build()
 for cmd,fn in [("start_task",start),("pause_task",pause),("resume_task",resume),("stop_task",stop),("status",status),("renew",check),("traffic",traffic)]: app.add_handler(CommandHandler(cmd,fn))
 app.run_polling()
if __name__=="__main__": main()
