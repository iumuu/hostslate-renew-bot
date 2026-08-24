#!/usr/bin/env python3
import asyncio, json, logging, os, sqlite3, threading, time
from pathlib import Path
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from playwright.async_api import async_playwright

TOKEN=os.environ["TELEGRAM_BOT_TOKEN"].strip()
ALLOWED={x.strip() for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS","").split(",") if x.strip()}
INTERVAL=max(60,int(os.getenv("RENEW_INTERVAL_MINUTES","60"))*60)
AUTO_RENEW=os.getenv("AUTO_RENEW","NO").upper()=="YES"
AUTO_PAY=os.getenv("AUTO_PAY","NO").upper()=="YES"
MAX_AMOUNT=float(os.getenv("MAX_RENEW_AMOUNT","0"))
PAYMENT_PROVIDER=os.getenv("PAYMENT_PROVIDER","balance")
LOGIN_USER=os.getenv("HOSTSLATE_USERNAME","")
LOGIN_PASSWORD=os.getenv("HOSTSLATE_PASSWORD","")
BASE="https://www.hostslate.com"
DATA=Path(os.getenv("DATA_DIR","/app/data")); PROFILE=DATA/"hostslate-profile"; DB=DATA/"state.db"
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
lock=threading.Lock(); state={"running":False,"paused":False,"busy":False,"phase":"空闲","current":"-","total":0,"done":0,"next_run":"-","last":"未执行"}
runtime_app=None
progress_messages={}

def progress_text():
 total=state["total"]; done=state["done"]
 if total:
  pct=int(done*100/total); width=10; filled=int(width*done/total)
  bar="🟩"*filled+"⬜"*(width-filled)
  progress=f"{bar} {pct}%  ({done}/{total})"
 else: progress="⏳ 等待获取实例数量"
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
 await update.message.reply_text("🟢 *续费循环已启动*\n\n进度会持续更新在同一条消息中。",parse_mode="Markdown")
 await progress("等待下一轮任务")
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
async def check(update,context):
 if not allowed(update): return
 await update.message.reply_text("正在执行一次检查……")
 was_running=state["running"]; state["running"]=True
 result=await renew_once()
 state["running"]=was_running; state["last"]=result; await update.message.reply_text(result)
async def renew_once():
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
async def loop(app):
 while True:
  await asyncio.sleep(INTERVAL)
  state["next_run"]="现在"
  if state["running"] and not state["paused"]:
   state["last"]=await renew_once()
   for uid in ALLOWED:
    try: await app.bot.send_message(int(uid),state["last"])
    except Exception: pass
  state["next_run"]=f"约 {INTERVAL//60} 分钟后"
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
 ])
 asyncio.create_task(loop(app))
def main():
 initdb(); app=Application.builder().token(TOKEN).post_init(post_init).build()
 for cmd,fn in [("start_task",start),("pause_task",pause),("resume_task",resume),("stop_task",stop),("status",status),("renew",check)]: app.add_handler(CommandHandler(cmd,fn))
 app.run_polling()
if __name__=="__main__": main()
