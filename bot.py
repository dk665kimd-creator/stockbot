"""한국 주식 컨센서스 텔레그램 봇.

종목명 또는 종목코드를 보내면 현재가, 증권사 목표주가(평균), 투자의견,
실적 컨센서스(추정치), 최근 증권사 리포트를 보여줍니다.
데이터 출처: 네이버 증권
"""
import asyncio
import logging
import os
import re
from io import StringIO

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stockbot")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
TIMEOUT = 10


def get_json(url: str, **params):
    r = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def get_html(url: str, **params) -> str:
    r = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.content.decode("euc-kr", errors="ignore")


# ---------------------------------------------------------------- 종목 검색
def resolve_stock(query: str):
    """종목명/코드 -> (코드, 종목명). 못 찾으면 None."""
    query = query.strip()
    if re.fullmatch(r"[0-9A-Za-z]{6}", query) and any(c.isdigit() for c in query):
        code = query.upper()
        try:
            name = get_json(f"https://m.stock.naver.com/api/stock/{code}/basic").get("stockName", code)
            return code, name
        except Exception:
            return None
    try:
        data = get_json(
            "https://m.stock.naver.com/front-api/search/autoComplete",
            query=query,
            target="stock",
        )
        items = data.get("result", {}).get("items", [])
        items = [i for i in items if i.get("nationCode", "KOR") == "KOR"] or items
        if items:
            return items[0]["code"], items[0]["name"]
    except Exception as e:
        log.warning("검색 실패: %s", e)
    return None


# ---------------------------------------------------------------- 데이터 조회
def fetch_price(code: str) -> dict:
    d = get_json(f"https://m.stock.naver.com/api/stock/{code}/basic")
    return {
        "name": d.get("stockName", code),
        "price": d.get("closePrice", "-"),
        "diff": d.get("compareToPreviousClosePrice", ""),
        "ratio": d.get("fluctuationsRatio", ""),
        "time": d.get("localTradedAt", ""),
    }


def fetch_target(code: str) -> dict:
    d = get_json(f"https://m.stock.naver.com/api/stock/{code}/integration")
    c = d.get("consensusInfo") or {}
    return {
        "target": c.get("priceTargetMean"),
        "recomm": c.get("recommMean"),
        "date": c.get("createDate"),
    }


def fetch_earnings(code: str):
    """네이버 증권 '기업실적분석' 표에서 연간/분기 실적(추정치 E 포함)."""
    html = get_html("https://finance.naver.com/item/main.naver", code=code)
    tables = pd.read_html(StringIO(html), match="매출액")
    df = tables[0]
    first = df.columns[0]
    df = df.set_index(first)

    def group(c):  # 상위 헤더(최근 연간 실적 / 최근 분기 실적)
        return str(c[-2]) if isinstance(c, tuple) and len(c) >= 2 else ""

    def period(c):  # 기간 (예: 2026.12(E))
        return str(c[-1] if isinstance(c, tuple) else c).strip()

    est = [c for c in df.columns if "(E)" in period(c)]
    # 분기 추정치 우선, 없으면 전체 추정치 중 가까운 것
    quarterly = [c for c in est if "분기" in group(c)]
    chosen = (quarterly or est)[:2]

    rows = {}
    for label in ["매출액", "영업이익", "당기순이익"]:
        if label in df.index:
            rows[label] = df.loc[label]
    periods = [period(c) for c in chosen]
    data = {
        label: {period(c): s[c] for c in chosen} for label, s in rows.items()
    }
    return data, periods


def fetch_reports(code: str, limit: int = 5):
    html = get_html(
        "https://finance.naver.com/research/company_list.naver",
        searchType="itemCode",
        itemCode=code,
    )
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.select("table.type_1 tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        a = tds[1].find("a")
        if not a:
            continue
        link = "https://finance.naver.com/research/" + a["href"]
        out.append(
            {
                "title": a.get_text(strip=True),
                "broker": tds[2].get_text(strip=True),
                "date": tds[4].get_text(strip=True),
                "link": link,
            }
        )
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- 메시지 생성
def esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def to_num(s):
    try:
        return float(str(s).replace(",", ""))
    except Exception:
        return None


def build_message(code: str, name: str) -> str:
    lines = []

    price_num = None
    try:
        p = fetch_price(code)
        price_num = to_num(p["price"])
        lines.append(f"<b>{esc(p['name'])}</b> ({code})")
        lines.append(f"현재가 <b>{esc(p['price'])}원</b>  ({esc(p['diff'])} / {esc(p['ratio'])}%)")
    except Exception as e:
        log.warning("가격 조회 실패: %s", e)
        lines.append(f"<b>{esc(name)}</b> ({code})")
        lines.append("현재가 조회 실패")

    try:
        t = fetch_target(code)
        if t["target"]:
            tn = to_num(t["target"])
            s = f"\n🎯 목표주가(평균) <b>{esc(t['target'])}원</b>"
            if tn and price_num:
                s += f"  (상승여력 {((tn / price_num) - 1) * 100:+.1f}%)"
            lines.append(s)
            if t["recomm"]:
                lines.append(f"투자의견 평균 {esc(t['recomm'])} / 5.0  (기준일 {esc(t['date'])})")
        else:
            lines.append("\n🎯 목표주가 컨센서스 없음")
    except Exception as e:
        log.warning("목표가 조회 실패: %s", e)

    try:
        earn, cols = fetch_earnings(code)
        if earn and cols:
            lines.append("\n📊 <b>분기 실적 컨센서스(추정)</b>")
            names = {"매출액": "매출액", "영업이익": "영업이익", "당기순이익": "순이익"}
            for col in cols:
                lines.append(f"[{esc(col)}]")
                for label, vals in earn.items():
                    v = vals.get(col)
                    if v is not None and str(v) != "nan":
                        lines.append(f"  {names[label]}: {esc(v)}억원")
        else:
            lines.append("\n📊 실적 추정치 없음")
    except Exception as e:
        log.warning("실적 조회 실패: %s", e)

    try:
        reports = fetch_reports(code)
        if reports:
            lines.append("\n📝 <b>최근 증권사 리포트</b>")
            for r in reports:
                lines.append(
                    f"• {esc(r['date'])} {esc(r['broker'])}\n"
                    f"  <a href=\"{esc(r['link'])}\">{esc(r['title'])}</a>"
                )
    except Exception as e:
        log.warning("리포트 조회 실패: %s", e)

    lines.append("\n<i>출처: 네이버 증권 · 투자 판단의 책임은 본인에게 있습니다.</i>")
    return "\n".join(lines)


# ---------------------------------------------------------------- 핸들러
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "종목명이나 종목코드를 보내주세요.\n예) 삼성전자, 005930\n\n"
        "현재가, 목표주가, 실적 컨센서스, 최근 리포트를 알려드려요."
    )


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    await update.message.chat.send_action("typing")
    found = await asyncio.to_thread(resolve_stock, text)
    if not found:
        await update.message.reply_text("종목을 찾지 못했어요. 정확한 종목명이나 6자리 코드를 입력해 주세요.")
        return
    code, name = found
    msg = await asyncio.to_thread(build_message, code, name)
    await update.message.reply_text(
        msg, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN 환경변수를 설정하세요 (.env 파일).")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    # Render 같은 클라우드: 공개 주소(RENDER_EXTERNAL_URL)가 있으면 웹훅 방식,
    # 없으면(내 PC) 기존 폴링 방식으로 실행
    base_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")
    if base_url:
        port = int(os.environ.get("PORT", "10000"))
        log.info("봇 시작 (웹훅 모드, 포트 %s)", port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=token,
            webhook_url=f"{base_url.rstrip('/')}/{token}",
        )
    else:
        log.info("봇 시작 (폴링 모드)")
        app.run_polling()


if __name__ == "__main__":
    main()
