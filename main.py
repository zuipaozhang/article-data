"""
微信公众号文章采集脚本
从飞书多维表格读取待采集链接 → 采集文章数据 → 回写飞书

GitHub Actions 定时运行，每周五 15:00 (北京时间)
"""

import os
import re
import time
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup

# ========== 配置（从环境变量读取） ==========
APP_ID = os.environ["FEISHU_APP_ID"]
APP_SECRET = os.environ["FEISHU_APP_SECRET"]
APP_TOKEN = os.environ["FEISHU_APP_TOKEN"]
TABLE_ID = os.environ["FEISHU_TABLE_ID"]
WECHAT_COOKIE = os.environ.get("WECHAT_COOKIE", "")

# 飞书 API 地址
FEISHU_AUTH_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
FEISHU_RECORDS_URL = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ========== 飞书 API ==========

def get_tenant_token() -> str:
    """获取飞书 tenant_access_token"""
    resp = requests.post(
        FEISHU_AUTH_URL,
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取飞书 token 失败: {data}")
    return data["tenant_access_token"]


def fetch_all_records(token: str) -> list[dict]:
    """分页获取数据表全部记录"""
    all_records = []
    page_token = None

    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token

        resp = requests.get(
            FEISHU_RECORDS_URL,
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=30,
        )
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"查询记录失败: {data}")

        items = data.get("data", {}).get("items", [])
        all_records.extend(items)

        if not data.get("data", {}).get("has_more"):
            break
        page_token = data["data"]["page_token"]

    return all_records


def filter_pending(records: list[dict]) -> list[dict]:
    """筛选「待采集」的记录：采集状态为空 或 等于"待采集" """
    pending = []
    for r in records:
        status = r.get("fields", {}).get("采集状态") or ""
        if not status or status == "待采集":
            pending.append(r)
    return pending


def update_record(token: str, record_id: str, fields: dict) -> bool:
    """更新一条飞书记录，fields 用字段名作为 key"""
    resp = requests.put(
        f"{FEISHU_RECORDS_URL}/{record_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"fields": fields},
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        log.error(f"更新记录失败 [{record_id}]: {data}")
        return False
    return True


# ========== 微信文章采集 ==========

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _get_rendered_html(url: str) -> tuple[str | None, dict]:
    """用 Playwright 渲染页面。同时拦截 getappmsgext API 获取阅读量/在看数。

    返回 (html, stats) 其中 stats 直接包含 read_num / like_num
    """
    stats: dict = {}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright 未安装，回退到 requests 模式")
        return None, {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ])
            context = browser.new_context(
                user_agent=USER_AGENT,
                locale="zh-CN",
            )

            # 注入 Cookie（修正 domain 为 mp.weixin.qq.com）
            if WECHAT_COOKIE:
                cookies = []
                for item in WECHAT_COOKIE.split("; "):
                    if "=" in item:
                        key, val = item.split("=", 1)
                        cookies.append({
                            "name": key,
                            "value": val,
                            "domain": "mp.weixin.qq.com",
                            "path": "/",
                        })
                context.add_cookies(cookies)

            page = context.new_page()

            # 拦截 getappmsgext —— 微信文章数据 API
            def _on_response(response):
                if "getappmsgext" in response.url:
                    try:
                        body = response.json()
                        stat = body.get("appmsgstat", {})
                        if "read_num" in stat:
                            stats["read_num"] = stat["read_num"]
                            log.info(f"  [API] 阅读量={stat['read_num']}")
                        if "like_num" in stat:
                            stats["like_num"] = stat["like_num"]
                            log.info(f"  [API] 在看数={stat['like_num']}")
                        # 旧接口返回格式
                        if "old_like_num" in stat:
                            stats.setdefault("like_num", stat["old_like_num"])
                    except Exception:
                        pass

            page.on("response", _on_response)

            # 屏蔽图片/字体等无关资源，加速加载
            page.route(
                re.compile(r"\.(png|jpg|jpeg|gif|svg|woff2?|ttf|css)(\?.*)?$"),
                lambda route: route.abort(),
            )

            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 等待正文渲染
            try:
                page.wait_for_selector("#js_content", timeout=15000)
            except Exception:
                pass

            # 等待异步 API 返回（阅读量接口通常 1-3 秒返回）
            time.sleep(4)

            html = page.content()

            # 没拦截到 API，debug 一下页面底部
            if not stats:
                try:
                    debug_text = page.evaluate("""() => {
                        const el = document.getElementById('js_content');
                        if (!el) return 'no js_content';
                        let next = el.nextElementSibling;
                        let text = '';
                        for (let i = 0; i < 5 && next; i++) {
                            text += (next.className || next.tagName) + ': ' +
                                    (next.textContent || '').substring(0, 200) + '\\n';
                            next = next.nextElementSibling;
                        }
                        return text || 'no siblings after js_content';
                    }""")
                    log.info(f"  [DEBUG] js_content 后续节点: {debug_text[:500]}")
                except Exception as e:
                    log.info(f"  [DEBUG] evaluate 失败: {e}")

            browser.close()
            return html, stats

    except Exception as e:
        log.warning(f"Playwright 渲染失败，回退到 requests: {e}")
        return None, {}


def _fetch_html_requests(url: str) -> tuple[str | None, dict]:
    """用 requests 获取静态 HTML"""
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://mp.weixin.qq.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if WECHAT_COOKIE:
        headers["Cookie"] = WECHAT_COOKIE

    resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    if resp.status_code == 404:
        return None, {}
    if resp.status_code != 200:
        return None, {}
    return resp.text, {}


def fetch_article(url: str) -> dict:
    """采集单篇文章，优先用 Playwright 渲染获取阅读量/在看数"""
    # 1. 优先 Playwright（能拿到阅读量/在看数）
    html, api_stats = _get_rendered_html(url)
    if html is None:
        html, _ = _fetch_html_requests(url)

    if html is None:
        return {"_error": "HTTP 404", "_status": "链接失效"}

    # 检查是否被反爬拦截
    if "请输入验证码" in html or "环境异常" in html or "当前访问疑似黑客" in html:
        return {"_error": "触发微信反爬验证", "_status": "采集失败"}

    # 检查文章是否已删除 / 违规
    if "该内容已被发布者删除" in html or "此内容因违规无法查看" in html:
        return {"_error": "文章已删除或违规", "_status": "链接失效"}

    soup = BeautifulSoup(html, "html.parser")

    result = {}

    # --- 标题 ---
    title_el = (
        soup.find("h1", class_="rich_media_title")
        or soup.find(id="activity-name")
        or soup.find("h1")
    )
    result["标题"] = title_el.get_text(strip=True) if title_el else ""

    # --- 发布时间 ---
    time_el = soup.find(id="publish_time") or soup.find("em", id="publish_time")
    result["发布时间"] = time_el.get_text(strip=True) if time_el else ""

    # --- 正文 ---
    content_el = soup.find(id="js_content")
    if content_el:
        for tag in content_el.find_all(style=re.compile(r"visibility\s*:\s*hidden")):
            tag.decompose()
        for tag in content_el.find_all(["script", "style"]):
            tag.decompose()

        full_text = content_el.get_text(separator="\n")
        full_text = re.sub(r"\n{3,}", "\n\n", full_text).strip()
        result["正文内容"] = full_text
        result["正文摘要"] = full_text[:200] if len(full_text) > 200 else full_text
    else:
        result["正文内容"] = ""
        result["正文摘要"] = ""

    # --- 阅读量 & 在看数（优先 API 拦截，次选 HTML 解析）---
    result["阅读量"] = (
        str(api_stats.get("read_num", ""))
        if api_stats.get("read_num")
        else _extract_read_count(soup)
    )
    result["在看数"] = (
        str(api_stats.get("like_num", ""))
        if api_stats.get("like_num")
        else _extract_like_count(soup)
    )

    return result


def _extract_read_count(soup: BeautifulSoup) -> str:
    """从渲染后的 HTML 中提取阅读量"""
    # 阅读量常用的 DOM 特征
    selectors = [
        # 直接 id
        lambda s: s.find(id="read_num"),
        lambda s: s.find(id="readNum"),
        # class 包含 read_num
        lambda s: s.find(class_=re.compile(r"read_num", re.I)),
        # span 文本含"阅读"
        lambda s: s.find("span", string=re.compile(r"阅读\s*\d")),
        # 底部 meta 区域
        lambda s: s.find(class_=re.compile(r"rich_media_meta_text", re.I), string=re.compile(r"阅读")),
    ]
    for selector in selectors:
        try:
            el = selector(soup)
            if el:
                text = el.get_text(strip=True)
                num = _parse_number(text)
                if num:
                    return num
        except Exception:
            continue

    # script 中的 JSON 数据
    for script in soup.find_all("script"):
        if script.string:
            for key in ["read_num", "readNum", "read_count", "readCount"]:
                match = re.search(rf'"{key}"\s*:\s*(\d+)', script.string)
                if match:
                    return match.group(1)

    return ""


def _extract_like_count(soup: BeautifulSoup) -> str:
    """从渲染后的 HTML 中提取在看数"""
    selectors = [
        lambda s: s.find(class_=re.compile(r"like_num|likeNum", re.I)),
        lambda s: s.find("span", string=re.compile(r"在看\s*\d")),
        lambda s: s.find(class_=re.compile(r"rich_media_meta_text", re.I), string=re.compile(r"在看")),
    ]
    for selector in selectors:
        try:
            el = selector(soup)
            if el:
                text = el.get_text(strip=True)
                num = _parse_number(text)
                if num:
                    return num
        except Exception:
            continue

    for script in soup.find_all("script"):
        if script.string:
            for key in ["like_num", "likeNum", "like_count", "likeCount"]:
                match = re.search(rf'"{key}"\s*:\s*(\d+)', script.string)
                if match:
                    return match.group(1)

    return ""


def _parse_number(text: str) -> str:
    """从文本中提取数字，如 '阅读 1.2万' → '12000'"""
    match = re.search(r"[\d,\.]+", text)
    if not match:
        return ""
    num_str = match.group().replace(",", "")

    if "万" in text:
        try:
            return str(int(float(num_str) * 10000))
        except ValueError:
            pass

    return num_str


# ========== 主流程 ==========

def main():
    log.info("=" * 50)
    log.info("微信公众号采集脚本 启动")
    log.info("=" * 50)

    # 1. 鉴权
    log.info("获取飞书 token ...")
    token = get_tenant_token()

    # 2. 拉全量记录 → 筛选待采集
    log.info("查询飞书数据表 ...")
    all_records = fetch_all_records(token)
    pending = filter_pending(all_records)

    log.info(f"表格共 {len(all_records)} 条记录，其中 {len(pending)} 条待采集")

    if not pending:
        log.info("没有需要采集的记录，退出")
        return

    # 3. 逐条采集
    success = 0
    fail = 0

    for i, record in enumerate(pending):
        record_id = record.get("record_id", "")
        fields = record.get("fields", {})
        url = (fields.get("原文链接") or "").strip()
        name = fields.get("公众号名称", "")

        log.info(f"[{i + 1}/{len(pending)}] {name} | {url[:60]}...")

        if not url:
            log.warning("  ⚠ 原文链接为空，跳过")
            fail += 1
            continue

        # 请求间隔，避免触发频率限制
        if i > 0:
            delay = 3 + (i % 7)
            time.sleep(delay)

        # 采集
        try:
            data = fetch_article(url)
        except requests.RequestException as e:
            log.error(f"  ✗ 网络请求异常: {e}")
            update_record(token, record_id, {
                "采集状态": "采集失败",
                "采集时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            fail += 1
            continue
        except Exception as e:
            log.error(f"  ✗ 采集异常: {e}")
            update_record(token, record_id, {
                "采集状态": "采集失败",
                "采集时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            fail += 1
            continue

        # 判断采集结果
        if data.get("_error"):
            log.warning(f"  ✗ {data['_error']}")
            update_record(token, record_id, {
                "采集状态": data.get("_status", "采集失败"),
                "采集时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            fail += 1
            continue

        # 构建回写数据
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        update_fields = {
            "采集状态": "已采集",
            "采集时间": now,
        }

        for key in ["标题", "发布时间", "正文内容", "正文摘要", "阅读量", "在看数"]:
            if data.get(key):
                update_fields[key] = data[key]

        if update_record(token, record_id, update_fields):
            success += 1
            title_preview = (data.get("标题") or "")[:30]
            read = data.get("阅读量", "")
            like = data.get("在看数", "")
            extras = ", ".join(filter(None, [
                f"阅读 {read}" if read else "",
                f"在看 {like}" if like else "",
            ]))
            log.info(f"  ✓ {title_preview}  {extras}")
        else:
            fail += 1

    log.info("=" * 50)
    log.info(f"采集完成：成功 {success} 条，失败 {fail} 条")
    log.info("=" * 50)


if __name__ == "__main__":
    main()
