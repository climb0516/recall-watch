# Created by 青云 on 2026/10/21.
"""消费避坑数据抓取：缺陷产品召回（消费品 / 汽车）、食品抽检不合格（总局 / 浙江）、产品质量国抽不合格、中消协消费警示。

只用标准库。政府站点大多是 jpaas CMS：栏目页是空壳，列表走 /api-gateway/jpaas-publish-server/front/page/build/unit
接口，详情页是静态 HTML。增量抓取：已在 data.json 里的条目（按详情页 URL）不再请求。
"""
import html, io, json, re, socket, time, urllib.parse, urllib.request, zipfile
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

ROOT = Path(__file__).parent
DATA_FILE = ROOT / "data.json"
CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
KEEP_DAYS = 365                 # 只保留一年内的数据
KEEP_DAYS_GUOCHOU = 730         # 国抽一年只发几期，保留两年
MAX_NEW_DETAILS = 400           # 每个来源单次最多新抓的详情页，首轮分几次补齐，避免一次请求太多
PAUSE = 0.4                     # 详情页请求间隔（秒）

SAMR = "https://www.samr.gov.cn"
DPRC = "https://www.samrdprc.org.cn"
ZJAMR = "https://zjamr.zj.gov.cn"

# 部分站点只对国内解析，Actions 的海外 DNS 查不到：系统 DNS 失败时改问阿里 DoH（带国内 EDNS 网段），
# 只替换解析结果，TLS 的 SNI 和 Host 仍是原域名
_sys_getaddrinfo = socket.getaddrinfo
_doh_cache = {}


def _doh_resolve(host):
    if host not in _doh_cache:
        url = "https://dns.alidns.com/resolve?" + urllib.parse.urlencode(
            {"name": host, "type": "A", "edns_client_subnet": "115.238.0.0/24"})
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=10) as r:
            ans = json.loads(r.read().decode()).get("Answer", [])
        _doh_cache[host] = [a["data"] for a in ans if a.get("type") == 1]
    return _doh_cache[host]


def _getaddrinfo(host, port, *args, **kw):
    try:
        return _sys_getaddrinfo(host, port, *args, **kw)
    except socket.gaierror:
        if not isinstance(host, str) or host == "dns.alidns.com":
            raise
        ips = _doh_resolve(host)
        if not ips:
            raise
        return _sys_getaddrinfo(ips[0], port, *args, **kw)


socket.getaddrinfo = _getaddrinfo


def fetch_bytes(url, timeout=25, retries=2):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for i in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception:
            if i == retries:
                raise
            time.sleep(2 + i * 3)


def fetch(url, **kw):
    return fetch_bytes(url, **kw).decode("utf-8", "ignore")


def clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def page_text(h):
    """详情页 HTML → 按行的纯文本。"""
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", h)
    h = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", h)
    t = html.unescape(re.sub(r"<[^>]+>", "", h))
    lines = [re.sub(r"[ \t　\xa0]+", " ", x).strip() for x in t.split("\n")]
    return [x for x in lines if x]


def now_cst():
    return datetime.now(CST)


def cutoff(days=KEEP_DAYS):
    return (now_cst().date() - timedelta(days=days)).isoformat()


# ── jpaas 列表接口 ──────────────────────────────────────
_query_cache = {}


def jpaas_list(base, column_url, page, size=20):
    """返回 [(date, title, absolute_url)]。"""
    if column_url not in _query_cache:
        t = fetch(column_url)
        qd = re.search(r'queryData="([^"]+)"', t).group(1)
        _query_cache[column_url] = json.loads(html.unescape(qd).replace("'", '"'))
    q = dict(_query_cache[column_url])
    q["paramJson"] = json.dumps({"pageNo": page, "pageSize": size})
    d = json.loads(fetch(f"{base}/api-gateway/jpaas-publish-server/front/page/build/unit?{urllib.parse.urlencode(q)}"))
    h = d["data"]["html"]
    # 各栏目模板不同（日期可能在同一个 li，也可能在相邻的 li / div），按「链接之后、下一个链接之前的第一个日期」配对
    out, seen = [], set()
    # 跳过「召回公告」「VIN码查询」这类附属短链接，只按正文条目配对日期
    anchors = [a for a in re.finditer(r'<a[^>]+href="([^"]*/art/[^"]+)"[^>]*>(.*?)</a>', h, re.S)
               if len(clean(a.group(2))) >= 8]
    for i, a in enumerate(anchors):
        nxt = anchors[i + 1].start() if i + 1 < len(anchors) else len(h)
        dt = re.search(r"(\d{4}-\d{2}-\d{2})", h[a.end():nxt])
        url = urllib.parse.urljoin(base + "/", html.unescape(a.group(1))).split("?type=")[0]
        title = clean(a.group(2))
        if not dt or url in seen:
            continue
        seen.add(url)
        out.append((dt.group(1), title, url))
    return out


def crawl_column(base, column_url, known, title_ok=lambda t: True, max_pages=60, keep_days=KEEP_DAYS):
    """翻页直到超出保留期，返回需要新抓详情的 [(date,title,url)]（新的在前）。"""
    todo, stop = [], cutoff(keep_days)
    for p in range(1, max_pages + 1):
        rows = jpaas_list(base, column_url, p)
        if not rows:
            break
        todo += [r for r in rows if r[0] >= stop and title_ok(r[1]) and r[2] not in known]
        # 一直翻到保留期为止（列表请求很轻）：首轮被 MAX_NEW_DETAILS 截断的旧条目，后续运行才能补齐
        if rows[-1][0] < stop:
            break
        time.sleep(PAUSE)
    return todo[:MAX_NEW_DETAILS]


# ── 分类 ───────────────────────────────────────────────
CATS = [
    ("儿童用品", r"儿童|婴|幼|童装|童鞋|童车|学生|书包|奶瓶|安抚|学步|尿"),
    ("玩具", r"玩具|积木|拼图|毛绒|气球|水枪|泡泡|象棋|魔方|卡牌|游戏机|遥控|仿真|手作|食玩|DIY|弹拖|甜甜圈"),
    ("电动车", r"电动自行车|电动车|电瓶车|平衡车|滑板车|电池组|充电器"),
    ("家电", r"电热|热水器|空调|冰箱|洗衣机|电饭|电磁炉|微波|烤箱|吹风|风扇|循环扇|取暖|暖风|加湿|净化|吸尘|电水壶|电热水壶|料理|榨汁|破壁|按摩|电熨|油烟机|燃气灶|灶具|热水壶|饮水机|除湿|电暖|厨房机械|蛋器|电火桶|加热器|加热棒|暖手宝|熨烫|制氧机|坐便器|电饼铛|净饮机|蚊|理发剪|筋膜枪|推脂机|水泵|水壶|消毒|燃具|炉"),
    ("汽车用品", r"制动液|润滑油|机油|轮胎|充电桩|车载|汽车"),
    ("电子数码", r"手机|电脑|计算机|打印机|充电宝|移动电源|耳机|音箱|平板|电源适配器|数据线|充电线|键盘|显示器|路由|手表|手环|相机|无人机"),
    ("灯具电器件", r"灯|插座|插头|开关|转换器|延长线|电线|电缆|断路器|排插"),
    ("美妆个护", r"化妆|护肤|洗发|沐浴|牙|剃须|面膜|香水|卫生巾|纸巾|湿巾|洗涤|洗衣液"),
    ("服装鞋帽", r"服|衣|衬衫|裤|裙|鞋|帽|袜|外套|T恤|羽绒|围巾|内衣|睡衣|套装"),
    ("家居日用", r"床|垫|被|枕|毛巾|家具|椅|桌|柜|锅|刀|杯|碗|餐具|保温|窗帘|门|锁|梯|晾衣|收纳|伞|眼镜|老视|消防|灭火|呼吸器|口罩|拖把|卫生纸|清洁剂|漆|拉杆箱|背包|手提|饰品|安全帽|救生|食物罐|眼罩|背提包"),
    ("户外运动", r"自行车|头盔|运动|健身|帐篷|登山|骑行|泳|滑雪|跑步机|跳绳|溯溪"),
    ("文具", r"簿|练习本|双行本|日格本|笔|文具|固体胶|强力胶|颜料|马克|文件袋|风琴包|笔袋|画|雕刻机"),
]


def guess_cat(text):
    for name, pat in CATS:
        if re.search(pat, text):
            return name
    return "其他"


FOOD_CATS = [                     # 顺序有讲究：先判自制餐饮、特膳、零食，再判宽泛的果蔬粮油
    ("餐饮自制", r"自制|餐饮|外卖|现制"),
    ("保健特膳", r"保健|特膳|特殊膳食|蛋白粉|营养|维生素|胶囊|压片"),
    ("糕点零食", r"饼|糕|月饼|面包|蛋糕|糖果|巧克力|薯片|膨化|坚果|瓜子|花生|炒货|蜜饯|果脯|话梅|青口梅|豆干|辣条|零食|馒头|包子|饼干"),
    ("乳制品", r"奶粉|乳|酸奶|牛奶|芝士|奶酪"),
    ("饮料酒水", r"饮料|饮用水|矿泉水|纯净水|泉水|茶|咖啡|酒|果汁|奶茶|可乐|汽水"),
    ("肉蛋水产", r"肉|鸡|鸭|鹅|牛|羊|猪|火腿|香肠|腊|蛋|鱼|虾|蟹|贝|海参|鱿|蛤|螺|水产|海鲜"),
    ("粮油调味", r"米|面|粉|油|酱|醋|盐|调味|鸡精|味精|花椒|淀粉|粉丝|粉条|料酒|糖"),
    ("蔬菜水果", r"菜|瓜|椒|豆芽|姜|葱|蒜|韭|芹|果|梅|桃|梨|橙|柑|桔|橘|枣|莓|葡萄|荔枝|龙眼|蕉|芒|柠檬|菌|菇|木耳"),
]


def food_cat(text):
    for name, pat in FOOD_CATS:
        if re.search(pat, text):
            return name
    return "其他食品"


PLATFORMS = ["淘宝闪购", "淘宝", "天猫", "京东", "拼多多", "抖音", "快手", "美团", "饿了么", "小红书", "唯品会",
             "1688", "苏宁", "得物", "微店", "盒马", "叮咚", "朴朴", "多多买菜", "有赞", "微信"]


def platform_of(text):
    for p in PLATFORMS:
        if p in (text or ""):
            return "淘宝" if p == "淘宝闪购" else p
    return ""


# ── 召回 ───────────────────────────────────────────────
def article_body(lines, title):
    """去掉页头导航，从「日前」或标题后的第一段正文开始。"""
    for i, x in enumerate(lines):
        if x.startswith("日前") or "召回计划" in x:
            return lines[i:]
    return lines


TABLE_KEYS = ["生产者名称", "产品名称", "品牌", "型号/规格", "生产起止日期", "涉及数量", "生产批号/批次", "产品描述及外观照片",
              "存在的缺陷", "可能导致的后果", "避免损害发生的应急处置方式", "具体召回措施", "召回负责机构", "召回联系方式",
              "召回进度安排", "其他需要报告的内容", "其他信息"]


def recall_table(lines):
    """部分地方局的召回公告是「字段名 / 值」两列表格，转成 dict（值可能跨多行）。"""
    out, key = {}, None
    for x in lines:
        if x in TABLE_KEYS:
            key = x
            out[key] = []
        elif key:
            out[key].append(x)
    return {k: "".join(v) for k, v in out.items()}


def parse_consumer_recall(date_s, title, url):
    all_lines = page_text(fetch(url))
    lines = article_body(all_lines, title)
    body = "\n".join(lines)
    m = re.match(r"【(?P<prov>[^】]+)】(?P<co>.+?)(?:扩大)?召回(?:部分)?(?P<rest>.+)$", title)
    prov, co, rest = (m.group("prov"), m.group("co"), m.group("rest")) if m else ("", "", title)
    bm = re.match(r"(?P<brand>.+?)牌(?P<prod>.+)$", rest)
    brand, prod = (bm.group("brand"), bm.group("prod")) if bm else ("", rest)
    prod = re.sub(r"产品$", "", prod) or prod
    tb = recall_table(all_lines) if "生产者名称" in all_lines else {}
    if tb:                                        # 表格式公告
        q = re.match(r"([\d,，.万]+)\s*(\S{0,3})", tb.get("涉及数量", ""))
        return {
            "kind": "consumer", "date": date_s, "title": title, "url": url, "province": prov,
            "company": tb.get("生产者名称") or co, "brand": tb.get("品牌") or brand, "product": prod,
            "cat": guess_cat(prod + title), "qty": q.group(1).replace(",", "").replace("，", "") if q else "",
            "unit": q.group(2) if q else "", "model": clean(tb.get("型号/规格", ""))[:120],
            "produced": tb.get("生产起止日期", ""),
            "defect": clean(tb.get("存在的缺陷", "") + tb.get("可能导致的后果", ""))[:300],
            "remedy": clean(tb.get("具体召回措施", ""))[:200],
        }
    qty = re.search(r"涉及数量(?:为|共)?\s*([\d,，.万]+)\s*([^\s，。、；）)]{0,3})", body) \
        or re.search(r"共计\s*([\d,，]+)\s*([件台个套只双辆把张盒箱条顶副组片支瓶])", body)
    qn, qu = (qty.group(1).replace(",", "").replace("，", ""), qty.group(2)) if qty else ("", "")
    if not qty:                                   # 分货号列出「数量60双、……数量60双」时求和
        parts = re.findall(r"数量[为：:]?\s*([\d,]+)\s*([件台个套只双把张盒箱条顶副组片支瓶])", body)
        if parts:
            qn, qu = str(sum(int(n.replace(",", "")) for n, _ in parts)), parts[0][1]
    model = re.search(r"[（(]型号(?:/规格)?[：:]\s*([^）)]{1,120})[）)]", body)
    produced = re.search(r"召回(\d{4}年\d{1,2}月\d{1,2}日至\d{4}年\d{1,2}月\d{1,2}日)期间", body)
    sentences = re.split(r"(?<=。)", body)
    defect = next((x for x in sentences if "本次召回范围内" in x or "由于" in x or "隐患" in x or "可能" in x and "风险" in x), "")
    remedy = next((x for x in sentences if re.search(r"将(?:免费|为)", x)), "")
    return {
        "kind": "consumer", "date": date_s, "title": title, "url": url, "province": prov, "company": co,
        "brand": brand, "product": prod, "cat": guess_cat(prod + title),
        "qty": qn, "unit": qu,
        "model": clean(model.group(1))[:120] if model else "", "produced": produced.group(1) if produced else "",
        "defect": clean(defect)[:300], "remedy": clean(remedy)[:200],
    }


def parse_auto_recall(date_s, title, url):
    lines = article_body(page_text(fetch(url)), title)
    body = "\n".join(lines)
    m = re.match(r"(?P<co>.+?)(?:扩大)?召回", title)
    co, prod = (m.group("co") if m else ""), auto_product(title)
    nums = re.findall(r"召回编号\s*([A-Z0-9]+)", body)
    qtys = [int(x.replace(",", "")) for x in re.findall(r"共计\s*([\d,]+)\s*辆", body)]
    defect = next((s for s in re.split(r"(?<=。)", body) if "由于" in s or "可能" in s and "风险" in s), "")
    remedy = next((s for s in re.split(r"(?<=。)", body) if re.search(r"将为|免费", s)), "")
    start = re.search(r"自(\d{4}年\d{1,2}月\d{1,2}日)起", body)
    return {
        "kind": "auto", "date": date_s, "title": title, "url": url, "company": co, "brand": brand_of_car(co, prod),
        "product": prod, "cat": "汽车", "qty": str(sum(qtys)) if qtys else "", "unit": "辆",
        "codes": nums[:6], "start": start.group(1) if start else "",
        "defect": clean(defect)[:300], "remedy": clean(remedy)[:200],
    }


CAR_BRANDS = ["讴歌", "本田", "丰田", "雷克萨斯", "日产", "英菲尼迪", "马自达", "三菱", "斯巴鲁", "铃木", "奔驰", "AMG", "宝马",
              "MINI", "奥迪", "大众", "保时捷", "斯柯达", "路虎", "捷豹", "沃尔沃", "福特", "林肯", "凯迪拉克", "别克", "雪佛兰",
              "特斯拉", "现代", "起亚", "极氪", "领克", "吉利", "比亚迪", "腾势", "仰望", "方程豹", "理想", "蔚来", "小鹏", "问界",
              "赛力斯", "小米", "长安", "深蓝", "阿维塔", "长城", "哈弗", "坦克", "魏牌", "欧拉", "奇瑞", "星途", "捷途", "广汽",
              "埃安", "传祺", "上汽", "荣威", "名爵", "智己", "五菱", "宝骏", "东风", "岚图", "日产", "一汽", "红旗", "奔腾",
              "零跑", "哪吒", "极狐", "北京", "江淮", "Jeep", "Smart", "smart", "法拉利", "兰博基尼", "玛莎拉蒂", "宾利",
              "劳斯莱斯", "阿斯顿", "迈凯伦", "雷诺", "标致", "雪铁龙", "DS", "依维柯", "五十铃", "哈雷", "杜卡迪", "春风", "九号", "雅迪"]


def auto_product(title):
    """「X公司召回部分进口和国产讴歌汽车」→「讴歌（进口和国产）」；只写「进口汽车」时保留产地说明。"""
    m = re.match(r".+?(?:扩大)?召回(?:部分)?(.+?)(?:汽车|车辆)?$", title)
    prod = m.group(1) if m else title
    origin = re.match(r"^(进口和国产|进口及国产|进口|国产)", prod)
    rest = prod[origin.end():].strip("、 ") if origin else prod
    if not rest:
        return f"{origin.group(1)}车辆（多车型）"
    return f"{rest}（{origin.group(1)}）" if origin else rest


CAR_ALIAS = {"AMG": "奔驰", "MINI": "宝马", "Smart": "smart"}


def brand_of_car(co, prod):
    for b in CAR_BRANDS:
        if b in prod:
            return CAR_ALIAS.get(b, b)
    for b in CAR_BRANDS:
        if b in co:
            return b
    return ""


# ── 食品抽检：总局（xlsx 附件，带商标）────────────────────
def xlsx_rows(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        x = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
        for si in re.findall(r"<si>(.*?)</si>", x, re.S):
            ss.append(html.unescape("".join(re.findall(r"<t[^>]*>(.*?)</t>", si, re.S))))
    sheet = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml", n))[0]
    x = z.read(sheet).decode("utf-8", "ignore")
    rows = []
    for r in re.findall(r"<row[^>]*>(.*?)</row>", x, re.S):
        row = {}
        for ref, attrs, body in re.findall(r'<c r="([A-Z]+)\d+"([^>]*?)(?:/>|>(.*?)</c>)', r, re.S):
            v = re.search(r"<v>(.*?)</v>", body or "")
            t = re.search(r't="(\w+)"', attrs)
            val = v.group(1) if v else "".join(re.findall(r"<t[^>]*>(.*?)</t>", body or ""))
            if t and t.group(1) == "s" and v:
                val = ss[int(val)]
            row[ref] = html.unescape(val).replace("\n", " ").strip()
        rows.append(row)
    return rows


def table(rows, must):
    """找到含 must 的表头行，返回 [dict(表头→值)]。表头里的空格去掉。"""
    for i, r in enumerate(rows):
        if any(must in v for v in r.values()):
            head = {k: re.sub(r"\s", "", v) for k, v in r.items()}
            out = []
            for rr in rows[i + 1:]:
                d = {head[k]: v for k, v in rr.items() if k in head and head[k]}
                if any(d.values()):
                    out.append(d)
            return out
    return []


def col(d, *names):
    for n in names:
        for k, v in d.items():
            if n in k and v and v != "/":
                return v
    return ""


def excel_date(v):
    if re.fullmatch(r"\d{5}", v or ""):
        return (date(1899, 12, 30) + timedelta(days=int(v))).isoformat()
    return v or ""


def parse_samr_food(date_s, title, url):
    h = fetch(url)
    items = []
    for href, name in re.findall(r'href="([^"?]+\.xlsx)(?:\?[^"]*)?"[^>]*>([^<]*)', h):
        if "不合格" not in name:
            continue
        group = re.sub(r"^附件\d+\s*|监督抽检不合格产品信息|\.xlsx$", "", name).strip()
        rows = table(xlsx_rows(fetch_bytes(urllib.parse.urljoin(SAMR + "/", html.unescape(href)))), "样品名称")
        for d in rows:
            prod = col(d, "样品名称")
            if not prod:
                continue
            note = col(d, "备注")
            items.append({
                "kind": "food", "src": "总局", "date": date_s, "notice": title, "url": url,
                "product": prod, "brand": col(d, "商标"), "maker": col(d, "标称生产企业名称"),
                "seller": col(d, "被抽样单位名称"), "platform": platform_of(note), "shop": (re.search(r"网店名称[:：]\s*([^；;]+)", note) or [None, ""])[1],
                "spec": col(d, "规格型号"), "produced": excel_date(col(d, "生产日期")),
                "items": col(d, "不合格项目"), "value": col(d, "检验值", "检验结果"), "std": col(d, "标准值"),
                "group": group, "cat": food_cat(group + prod), "region": "全国",
            })
        time.sleep(PAUSE)
    if not items:                      # 没有 xlsx 时从正文解析
        items = parse_food_text(page_text(h), date_s, title, url, "总局", "全国")
    return items


# ── 食品抽检：正文逐条（浙江省局，以及总局兜底）──────────────
ITEM_RE = re.compile(
    r"^(?:（[一二三四五六七八九十百]+）)?(?P<seller>[^，。]+?)(?:（(?P<shop>[^）]*)）)?"
    r"(?:在(?P<plat>[^，。]+?))?销售的、?(?:标称(?P<maker>[^，。]+?)(?:生产|监制|委托)[^，。]*?的)?"
    r"(?P<prod>[^，。]+?)，(?:其中)?(?P<items>.+?)(?:检验值)?(?:既)?(?:不符合|符合食品安全国家标准规定，但不符合)")


def parse_food_text(lines, date_s, title, url, src, region):
    items, group, seen = [], "", set()
    for x in lines:
        if re.match(r"^[一二三四五六七八九十]+、\S{2,12}$", x):
            group = x.split("、", 1)[1]
            continue
        m = ITEM_RE.match(x)
        if not m or x in seen:
            continue
        seen.add(x)
        prod = m.group("prod")
        items.append({
            "kind": "food", "src": src, "date": date_s, "notice": title, "url": url,
            "product": prod, "brand": "", "maker": m.group("maker") or "", "seller": m.group("seller"),
            "platform": platform_of(m.group("plat") or ""), "shop": (m.group("shop") or "").replace("店铺名为", ""),
            "items": m.group("items").rstrip("，"), "group": group, "cat": food_cat(prod),
            "region": region, "note": "复检维持" if "复检维持" in x else "",
        })
    return items


def parse_zj_food(date_s, title, url):
    return parse_food_text(page_text(fetch(url)), date_s, title, url, "浙江", "浙江")


# ── 产品质量国家监督抽查（xlsx 附件）────────────────────────
def parse_guochou(date_s, title, url):
    h = fetch(url)
    items = []
    for href, name in re.findall(r'href="([^"?]+\.xlsx)(?:\?[^"]*)?"[^>]*>([^<]*)', h):
        if "不合格产品" not in name:
            continue
        rows = table(xlsx_rows(fetch_bytes(urllib.parse.urljoin(SAMR + "/", html.unescape(href)))), "产品名称")
        for d in rows:
            prod = col(d, "产品名称")
            if not prod:
                continue
            kind_ = col(d, "产品种类")
            items.append({
                "kind": "product", "src": "国抽", "date": date_s, "notice": title, "url": url,
                "product": prod, "brand": col(d, "商标"), "maker": col(d, "标称生产单位", "生产单位"),
                "seller": col(d, "受检单位", "销售单位"), "platform": platform_of(col(d, "电商平台", "备注")),
                "spec": col(d, "规格型号"), "produced": col(d, "生产日期"), "items": col(d, "主要不合格项目", "不合格项目"),
                "group": kind_, "cat": guess_cat(kind_ + prod), "region": col(d, "所在地") or "全国",
            })
        time.sleep(PAUSE)
    return items


# ── 中消协消费警示 ───────────────────────────────────────
def cca_tips():
    out = []
    for page in ("11188.html", "11188_2.html"):
        t = fetch(f"https://cca.org.cn/channel/list/{page}")
        for href, d, title in re.findall(
                r'<a href="(https://cca\.org\.cn/single/\d+/\d+\.html)">\s*<span[^>]*>\s*([\d.]+)\s*</span>\s*([^<]+?)\s*</a>', t):
            out.append({"date": d.replace(".", "-"), "title": clean(title), "url": href})
        time.sleep(PAUSE)
    return out


# ── 汇总 ───────────────────────────────────────────────
def load_old():
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    old = load_old()
    stop = cutoff()
    recalls = [x for x in old.get("recalls", []) if x["date"] >= stop]
    fails = [x for x in old.get("fails", []) if x["date"] >= (cutoff(KEEP_DAYS_GUOCHOU) if x["src"] == "国抽" else stop)]
    notices = [x for x in old.get("notices", []) if x["date"] >= cutoff(KEEP_DAYS_GUOCHOU)]   # 已处理过的通报（含 0 条的）
    known = {x["url"] for x in recalls} | {x["url"] for x in notices}
    old_src = {s["id"]: s for s in old.get("sources", [])}
    sources = []

    def run(sid, name, base, column, parser, sink, count, title_ok=lambda t: True, is_notice=False, keep_days=KEEP_DAYS):
        added, err = 0, None
        try:
            todo = crawl_column(base, column, known, title_ok, keep_days=keep_days)
            for d, t, u in todo:
                try:
                    res = parser(d, t, u)
                except Exception as e:           # 单条失败不影响其他条
                    print(f"  ! {t[:30]} {type(e).__name__}: {e}")
                    continue
                if is_notice:
                    notices.append({"date": d, "title": t, "url": u, "src": name, "count": len(res)})
                    sink.extend(res)
                    added += len(res)
                else:
                    sink.append(res)
                    added += 1
                known.add(u)
                time.sleep(PAUSE)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:160]
        total = sum(1 for x in sink if count(x))
        sources.append({"id": sid, "name": name, "ok": err is None, "added": added,
                        "count": total, "error": err,
                        "updated": now_cst().isoformat(timespec="seconds") if err is None else old_src.get(sid, {}).get("updated")})
        print(f"{'✓' if err is None else '✗'} {name}: +{added}" + (f"  {err}" if err else ""))

    run("consumer_recall", "消费品召回", DPRC, f"{DPRC}/xfpzh/index.html", parse_consumer_recall, recalls,
        lambda x: x["kind"] == "consumer",
        title_ok=lambda t: "召回" in t)
    run("auto_recall", "汽车召回", DPRC, f"{DPRC}/zhxx/jdczh/jdczh/index.html", parse_auto_recall, recalls,
        lambda x: x["kind"] == "auto",
        title_ok=lambda t: "召回" in t)
    if not sources[-1]["ok"]:              # 召回中心不通时用总局镜像栏目
        sources.pop()
        run("auto_recall", "汽车召回（总局）", SAMR, f"{SAMR}/zw/zh/index.html", parse_auto_recall, recalls,
            lambda x: x["kind"] == "auto",
            title_ok=lambda t: "召回" in t)
    run("samr_food", "总局食品抽检", SAMR, f"{SAMR}/zw/zfxxgk/fdzdgknr/spcjs/index.html", parse_samr_food, fails,
        lambda x: x["src"] == "总局",
        title_ok=lambda t: "不合格" in t, is_notice=True)
    run("zj_food", "浙江食品抽检", ZJAMR, f"{ZJAMR}/col/col1228969898/index.html", parse_zj_food, fails,
        lambda x: x["src"] == "浙江",
        title_ok=lambda t: "抽检" in t, is_notice=True)
    run("guochou", "产品质量国抽", SAMR, f"{SAMR}/zljds/jdcc/index.html", parse_guochou, fails,
        lambda x: x["src"] == "国抽",
        title_ok=lambda t: "国家监督抽查" in t, is_notice=True, keep_days=KEEP_DAYS_GUOCHOU)
    try:
        tips = cca_tips()
        sources.append({"id": "cca", "name": "中消协消费警示", "ok": True, "added": 0, "count": len(tips),
                        "updated": now_cst().isoformat(timespec="seconds")})
        print(f"✓ 中消协消费警示: {len(tips)}")
    except Exception as e:
        tips = old.get("tips", [])
        sources.append({"id": "cca", "name": "中消协消费警示", "ok": False, "count": len(tips),
                        "error": f"{type(e).__name__}: {e}"[:160], "updated": old_src.get("cca", {}).get("updated")})
        print(f"✗ 中消协消费警示: {e}")

    # 品类每次都按最新关键词重算，调整关键词后旧数据也跟着变；通报标题只在 notices 里存一份
    for x in recalls:
        if x["kind"] == "consumer":
            x["cat"] = guess_cat(x["product"] + x["title"])
        else:
            x["product"] = auto_product(x["title"])
            x["brand"] = brand_of_car(x["company"], x["product"])
    for x in fails:
        x["cat"] = food_cat(x.get("group", "") + x["product"]) if x["kind"] == "food" else guess_cat(x.get("group", "") + x["product"])
        x.pop("notice", None)

    recalls.sort(key=lambda x: x["date"], reverse=True)
    fails.sort(key=lambda x: x["date"], reverse=True)
    notices.sort(key=lambda x: x["date"], reverse=True)
    data = {
        "updated": now_cst().isoformat(timespec="seconds"),
        "keep_days": KEEP_DAYS,
        "recalls": recalls, "fails": fails, "notices": notices, "tips": tips,
        "sources": sources,
    }
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"done: 召回 {len(recalls)} / 不合格 {len(fails)} / 通报 {len(notices)} / 提示 {len(tips)}")


if __name__ == "__main__":
    main()
