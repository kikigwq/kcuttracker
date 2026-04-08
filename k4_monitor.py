import requests
from bs4 import BeautifulSoup
import json
import logging
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 设置日志格式并写入文件
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename="k4jisung.log",  # 输出到日志文件
    filemode="a"
)

# 商品URL和站点名称映射
product_urls = {
    "https://jp.ktown4u.com/iteminfo?eve_no=43251223&goods_no=136891&grp_no=43251225": "jp",
    "https://www.ktown4u.com/iteminfo?eve_no=43251223&goods_no=136891&grp_no=43251225": "eng",
    "https://cn.ktown4u.com/iteminfo?eve_no=43251223&goods_no=136891&grp_no=43251225": "cn",
    "https://kr.ktown4u.com/iteminfo?eve_no=43251223&goods_no=136891&grp_no=43251225": "kr"
}

# 存储库存数据
last_quantities = {}
total_sales = 0  # 记录总销量

# ── 新增：JSON输出相关变量 ──────────────────────────
JSON_FILE = "data_k4.json"
_records = []
_trend = []
_channel_totals = {}
_revert_count = 0

def _write_json():
    stocks = [
        {
            "site": s,
            "quantity": last_quantities.get(u),
            "display": (f"{last_quantities[u]:+d}" if u in last_quantities else "—")
        }
        for u, s in product_urls.items()
    ]
    payload = {
        "team": "zb1",
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "total_sales": total_sales,
            "latest_qty": _records[-1]["qty_abs"] if _records else 0,
            "revert_count": _revert_count,
            "record_count": len(_records),
        },
        "current_stocks": stocks,
        "channel_totals": _channel_totals,
        "trend": _trend[-200:],
        "records": list(reversed(_records[-200:])),
    }
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
# ────────────────────────────────────────────────────


def create_session():
    """
    创建带有重试机制和请求头的请求会话
    """
    session = requests.Session()
    # 设置请求头，模拟浏览器
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "DNT": "1",  # Do Not Track
        "Upgrade-Insecure-Requests": "1"
    }
    session.headers.update(headers)

    # 设置重试机制
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def fetch_stock_data(url, session):
    """
    从指定URL获取商品名称和库存数据
    """
    try:
        # 发送HTTP请求
        response = session.get(url, timeout=5)
        response.raise_for_status()  # 抛出HTTP错误

        # 解析HTML
        soup = BeautifulSoup(response.text, "html.parser")
        script_tag = soup.find("script", {"id": "__NEXT_DATA__"})

        if not script_tag:
            logging.error(f"未找到包含JSON数据的<script>标签 for {url}")
            return None, None

        # 解析JSON数据
        try:
            json_data = json.loads(script_tag.string)
        except json.JSONDecodeError as e:
            logging.error(f"JSON解析失败 for {url}: {e}")
            return None, None

        # 提取productDetails
        try:
            page_props = json_data.get("props", {}).get("pageProps", {})
            product_details = page_props.get("productDetails")

            if not product_details:
                logging.error(f"未找到productDetails数据 for {url}")
                # 记录JSON数据以供调试
                logging.info(f"JSON数据: {json.dumps(json_data, indent=2, ensure_ascii=False)}")
                return None, None

            product_name = product_details.get("productName")
            quantity = product_details.get("quantity")

            if product_name is None or quantity is None:
                logging.error(f"商品名称或库存数据缺失 for {url}")
                return None, None

            return product_name, quantity

        except (KeyError, TypeError) as e:
            logging.error(f"提取productDetails时出错 for {url}: {e}")
            logging.info(f"JSON数据: {json.dumps(json_data, indent=2, ensure_ascii=False)}")
            return None, None

    except requests.exceptions.RequestException as e:
        logging.error(f"请求发生错误 for {url}: {e}")
        return None, None


def monitor_stock_changes():
    """
    监控库存变化并记录销量
    """
    global total_sales, _revert_count
    session = create_session()  # 创建带重试的会话

    while True:
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')  # 记录当前时间戳
        total_sales_change = 0  # 本次循环的销量变化总和
        sales_changes = {}  # 记录每个商品的销量变化
        had_change = False  # 新增

        for url, site_name in product_urls.items():
            product_name, current_quantity = fetch_stock_data(url, session)

            if product_name is None or current_quantity is None:
                logging.error(f"无法获取商品信息或库存数据 for {url} ({site_name})")
                continue

            # 首次获取库存时初始化
            if url not in last_quantities:
                last_quantities[url] = current_quantity
                sales_changes[url] = 0
                logging.info(f"{timestamp} - 站点: {site_name}, 商品名称: {product_name}, 初始库存: {current_quantity}")
                # 新增：写初始化记录
                # ★ 方案B：如果初始库存是负数，说明开售前已有销量，直接计入总销量
                init_sold = abs(current_quantity) if current_quantity < 0 else 0
                if init_sold > 0:
                    total_sales += init_sold
                    _channel_totals[site_name] = _channel_totals.get(site_name, 0) + init_sold
                    _trend.append({"time": timestamp, "cumulative": total_sales})
                    logging.info(f"{timestamp} - 站点: {site_name}, 初始已售（负库存）: {init_sold}, 累计: {total_sales}")
                _records.append({"time": timestamp, "channel": site_name, "before": None, "after": current_quantity, "qty": 0, "qty_abs": init_sold, "type": "init"})
                had_change = True
            else:
                # 计算销量变化（库存减少表示销量增加）
                sales_change = last_quantities[url] - current_quantity
                sales_changes[url] = sales_change
                total_sales_change += sales_change

                # 新增：记录每笔变动
                if sales_change != 0:
                    rec_type = "normal" if sales_change > 0 else "revert"
                    if rec_type == "revert":
                        _revert_count += 1
                    else:
                        _channel_totals[site_name] = _channel_totals.get(site_name, 0) + sales_change
                        _trend.append({"time": timestamp, "cumulative": total_sales + sales_change})
                    _records.append({"time": timestamp, "channel": site_name, "before": last_quantities[url], "after": current_quantity, "qty": -sales_change, "qty_abs": abs(sales_change), "type": rec_type})
                    had_change = True

                # 更新当前库存
                last_quantities[url] = current_quantity

        # 如果有销量变化，记录数据
        if total_sales_change != 0:
            for url, site_name in product_urls.items():
                sales_change = sales_changes.get(url, 0)
                if sales_change != 0:
                    logging.info(f"{timestamp} - 站点: {site_name}, 销量变化: {sales_change}")
            # 更新总销量
            total_sales += total_sales_change
            logging.info(f"{timestamp} - 总销量: {total_sales}")
        else:
            # 无销量变化时不输出
            pass

        # 新增：有变化就写JSON
        if had_change:
            _write_json()

        # 每60秒检查一次库存
        time.sleep(5)


# 启动监控功能
if __name__ == "__main__":
    try:
        monitor_stock_changes()
    except KeyboardInterrupt:
        logging.info("监控程序被用户终止")
        _write_json()  # 新增：退出时保存
    except Exception as e:
        logging.error(f"监控程序发生未预期的错误: {e}")
