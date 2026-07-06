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

def fetch_article(url: str) -> dict:
    """采集单篇文章，返回字段数据"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://mp.weixin.qq.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if WECHAT_COOKIE:
        headers["Cookie"] = WECHAT_COOKIE

    resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)

    # 检查状态码
    if resp.status_code == 404:
        return {"_error": "链接失效", "_status": "链接失效"}
    if resp.status_code != 200:
        return {"_error": f"HTTP {resp.status_code}", "_status": "采集失败"}

    html = resp.text

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
        # 移除 hidden 干扰字符
        for tag in content_el.find_all(style=re.compile(r"visibility\s*:\s*hidden")):
            tag.decompose()
        # 移除 script / style
        for tag in content_el.find_all(["script", "style"]):
            tag.decompose()

        full_text = content_el.get_text(separator="\n")
        # 清理多余空行
        full_text = re.sub(r"\n{3,}", "\n\n", full_text).strip()
        result["正文内容"] = full_text
        result["正文摘要"] = full_text[:200] if len(full_text) > 200 else full_text
    else:
        result["正文内容"] = ""
        result["正文摘要"] = ""

    # --- 阅读量 ---
    result["阅读量"] = _extract_read_count(soup)

    # --- 在看数 ---
    result["在看数"] = _extract_like_count(soup)

    return result


def _extract_read_count(soup: BeautifulSoup) -> str:
    """尽力从 HTML 中提取阅读量（静态 HTML 中通常没有，需浏览器渲染）"""
    # 方法1：id 为 read_num
    for el_id in ["read_num", "readNum"]:
        el = soup.find(id=el_id)
        if el:
            text = el.get_text(strip=True)
            if text:
                return _parse_number(text)

    # 方法2：class 含 read
    for cls in ["read_num", "readNum", "rich_media_meta_text"]:
        for el in soup.find_all(class_=re.compile(cls, re.I)):
            text = el.get_text(strip=True)
            num = _parse_number(text)
            if num:
                return num

    # 方法3：script 中的数据
    for script in soup.find_all("script"):
        if script.string and "read_num" in script.string:
            match = re.search(r'"read_num"\s*:\s*(\d+)', script.string)
            if match:
                return match.group(1)

    return ""


def _extract_like_count(soup: BeautifulSoup) -> str:
    """尽力从 HTML 中提取在看数"""
    for cls in ["likeNum", "like_num"]:
        el = soup.find(class_=re.compile(cls, re.I))
        if el:
            text = el.get_text(strip=True)
            num = _parse_number(text)
            if num:
                return num

    for script in soup.find_all("script"):
        if script.string and "like_num" in script.string:
            match = re.search(r'"like_num"\s*:\s*(\d+)', script.string)
            if match:
                return match.group(1)

    return ""


def _parse_number(text: str) -> str:
    """从文本中提取数字，如 '阅读 1.2万' → '12000'"""
    # 直接提取数字部分
    match = re.search(r"[\d,\.]+", text)
    if not match:
        return ""
    num_str = match.group().replace(",", "")

    # 处理"万"单位
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
        url = fields.get("原文链接", "").strip()
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

        # 只把非空的字段写入
        for key in ["标题", "发布时间", "正文内容", "正文摘要", "阅读量", "在看数"]:
            if data.get(key):
                update_fields[key] = data[key]

        if update_record(token, record_id, update_fields):
            success += 1
            title_preview = (data.get("标题") or "")[:30]
            read = data.get("阅读量", "")
            log.info(f"  ✓ {title_preview}  {read if read else ''}")
        else:
            fail += 1

    log.info("=" * 50)
    log.info(f"采集完成：成功 {success} 条，失败 {fail} 条")
    log.info("=" * 50)


if __name__ == "__main__":
    main()
