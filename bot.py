#!/usr/bin/env python3
import asyncio, json, logging, os, sqlite3, threading, time
from pathlib import Path
from telegram import Update
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
lock=threading.Lock(); state={"running":False,"paused":False,"last":"未执行"}

def allowed(u): return not ALLOWED or str(u.effective_user.id) in ALLOWED
def initdb():
 DATA.mkdir(parents=True,exist_ok=True); sqlite3.connect(DB).execute("create table if not exists orders (key text primary key, result text, ts integer)").connection.commit()
async def msg(update,text):
 if allowed(update): await update.message.reply_text(text)
async def status(update,context): await msg(update,f"运行中: {state['running']}\n暂停: {state['paused']}\n最近结果: {state['last']}")
async def start(update,context):
 if not allowed(update): return
 with lock: state.update(running=True,paused=False)
 await update.message.reply_text("已启动循环任务。")
async def pause(update,context):
 if not allowed(update): return
 with lock: state["paused"]=True
 await update.message.reply_text("已暂停，不会开始新的续费操作。")
async def resume(update,context):
 if not allowed(update): return
 with lock: state.update(running=True,paused=False)
 await update.message.reply_text("已恢复循环任务。")
async def stop(update,context):
 if not allowed(update): return
 with lock: state.update(running=False,paused=False)
 await update.message.reply_text("已停止循环任务。")
async def check(update,context):
 if not allowed(update): return
 await update.message.reply_text("正在执行一次检查……")
 result=await renew_once()
 state["last"]=result; await update.message.reply_text(result)
async def renew_once():
 try:
  async with async_playwright() as p:
   browser=await p.chromium.launch_persistent_context(str(PROFILE),headless=True)
   page=await browser.new_page(); await page.goto(BASE+"/login",wait_until="domcontentloaded")
   await page.wait_for_url("**/portal/**",timeout=15000)
   await page.goto(BASE+"/portal/instances",wait_until="networkidle")
   buttons=page.get_by_text("续费",exact=False); n=await buttons.count()
   if not n: await browser.close(); return "未发现续费按钮。首次登录或人机验证可能需要手动完成。"
   done=0
   for i in range(n):
    if not AUTO_RENEW: continue
    key=f"{await buttons.nth(i).inner_text()}-{i}"
    con=sqlite3.connect(DB); old=con.execute("select 1 from orders where key=?",(key,)).fetchone(); con.close()
    if old: continue
    await buttons.nth(i).click(); await page.wait_for_timeout(1000)
    # Only free balance orders are eligible for automatic payment.
    body=(await page.locator("body").inner_text()).lower()
    if AUTO_PAY and MAX_AMOUNT == 0 and any(x in body for x in ["0.00", "￥0", "¥0", "免费", "免费"]):
     pay=page.get_by_text("余额支付",exact=False)
     if await pay.count():
      await pay.first.click(); await page.wait_for_timeout(1500)
      status="renew-paid" if not await page.get_by_text("支付失败",exact=False).count() else "payment-failed"
     else: status="payment-control-not-found"
    else: status="renew-clicked-awaiting-confirmation"
    con=sqlite3.connect(DB); con.execute("insert or replace into orders values(?,?,?)",(key,status,int(time.time()))); con.commit(); con.close(); done+=1
   await browser.close(); return f"发现 {n} 个续费入口，已处理 {done} 个；自动支付={AUTO_PAY}（支付仍需人工确认）。"
 except Exception as e: return "执行失败："+str(e)[:500]
async def loop(app):
 while True:
  await asyncio.sleep(INTERVAL)
  if state["running"] and not state["paused"]:
   state["last"]=await renew_once()
   for uid in ALLOWED:
    try: await app.bot.send_message(int(uid),state["last"])
    except Exception: pass
async def post_init(app): asyncio.create_task(loop(app))
def main():
 initdb(); app=Application.builder().token(TOKEN).post_init(post_init).build()
 for cmd,fn in [("start_task",start),("pause_task",pause),("resume_task",resume),("stop_task",stop),("status",status),("renew",check)]: app.add_handler(CommandHandler(cmd,fn))
 app.run_polling()
if __name__=="__main__": main()
