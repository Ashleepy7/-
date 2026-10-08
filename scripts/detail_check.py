"""查找功能闭卷评测（可判涨跌版）。

这一轮只测、只出报告，不碰 core/query.py 的任何检索逻辑。尺子做准了，
以后每次改检索才敢说“是真提升还是排序在抖”。

设计要点：
1) 用例不在脚本里，单独放 data/detail_cases.json（缺失就报错让先出题，绝不静默用内置题）。
2) 每条用例连跑 5 次 engine=qwen（--rounds 可改）——要的就是这几次不一样；规则版对照只跑 1 次。
3) 两个稳定性指标（口径与轮数无关，ROUNDS 默认 5）：
   至少一次命中 = 各轮里有一轮 top-5 命中；全轮命中 = 每一轮 top-5 都命中（保守口径）。
   两者差距大的条目列进「排序在抖」清单（模型给词不稳，不是代码问题，但必须先量出来）。
   命中率一律附 Wilson 95% 置信区间：样本小时能判涨跌、样本大时区间才收窄。
4) 通过门禁写进脚本，任一不满足就在报告开头打「不合格」。
5) 防开卷：句子与库内原话最长重合 ≥4 字的用例标「开卷·不计分」并从分母剔除。
6) 原始每轮结果另存 data/detail_raw.json，覆盖前把旧的追加进 history，别丢。

用法：python scripts/detail_check.py [--cases 用例文件] [--only 子串,子串] [--rounds N] [--tag 名字]
     --only 只跑题面包含该子串的题（连改多步时只量受影响的那几条，省 token）
     --tag 报告写成 detail_report_<tag>.txt / detail_raw_<tag>.json，不覆盖主基线
     不带参数就是全量：每条 qwen×3 + 规则×1，覆盖主报告。
     报告 data/detail_report.txt　原始 data/detail_raw.json　用例 data/detail_cases.json
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core import config, db, llm, query      # noqa: E402

# 绕开 llm 的解析缓存，用的是整体关掉（llm.set_cache(False)）而不是开头 clear 一次：
# 每条题要连跑 ROUNDS 轮，只 clear 一次的话第 2、3 轮量的仍是第 1 轮的复读，
# 「跑满几轮取最差」直接废掉。关掉后每轮都真发请求，代价是 token 照烧。
llm.set_cache(False)

ROUNDS = 5              # 每条题连跑几轮（qwen），--rounds 可改；扩样后保守口径按全轮命中


def _argv() -> dict:
    """极简命令行：只控制跑哪些题、跑几轮、写到哪个文件，不动任何判分口径。"""
    opts = {"cases": None, "only": [], "rounds": ROUNDS, "tag": ""}
    a = sys.argv[1:]
    i = 0
    while i < len(a):
        if a[i] == "--cases" and i + 1 < len(a):
            opts["cases"] = a[i + 1]
            i += 2
        elif a[i] == "--only" and i + 1 < len(a):
            opts["only"] = [x for x in a[i + 1].split(",") if x.strip()]
            i += 2
        elif a[i] == "--rounds" and i + 1 < len(a):
            opts["rounds"] = max(1, int(a[i + 1]))
            i += 2
        elif a[i] == "--tag" and i + 1 < len(a):
            opts["tag"] = a[i + 1].strip()
            i += 2
        else:
            sys.exit(f"不认识参数：{a[i]}（可用 --cases/--only/--rounds/--tag）")
    return opts


OPTS = _argv()
ROUNDS = OPTS["rounds"]                      # 全量跑仍用文件里的默认值，只数题时才变
_SUFFIX = f"_{OPTS['tag']}" if OPTS["tag"] else ""
REPORT = config.DATA_DIR / f"detail_report{_SUFFIX}.txt"
CASES = pathlib.Path(OPTS["cases"]) if OPTS["cases"] else config.DATA_DIR / "detail_cases.json"
RAW = config.DATA_DIR / f"detail_raw{_SUFFIX}.json"
LIMIT = 100              # 每次返回上限（够算 rank / 召回即可）
TOO_MANY_A = 8           # 唯一答案题结果超过这个数＝没区分度
TOPK = 5                 # 命中判定的名次窗口

# 上一版基线（写进报告开头对比，不随本轮变）
BASELINE = {
    "text": "上一版基线（13 条·每条只跑 1 次）",
    "A_top1": "5/8", "A_top5": "8/8（三轮全中）",
    "single_total_max": 10, "C_false": "1 条（滑雪→雪山）",
}

# 回归护栏：这几条不属于用例，是每次都必查的底线
GUARD_2019 = "2019年的照片"        # 期望 total=0
GUARD_NOLISTEN = "上面的照片"      # 期望 conds 为空 + “没听懂”提示


# ---------------------------------------------------------------- 小工具

def wlen(s: str) -> int:
    return sum(2 if ord(ch) > 127 else 1 for ch in s)


def pad(s: str, n: int) -> str:
    return s + " " * max(1, n - wlen(s))


def lcs(a: str, b: str) -> int:
    """最长公共连续片段：判断考题有没有照抄库内原话（防开卷）。"""
    best, prev = 0, [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """比例的 Wilson 95% 置信区间。样本小（如 A 组 17 题）时比正态近似稳，
    不会给出负下限；扩到 30 题后区间明显收窄，才敢说某个改动是真涨还是噪声。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def hay(rec: dict) -> str:
    return f"{rec.get('caption') or ''} {' '.join(json.loads(rec.get('tags') or '[]'))}"


def rank_of(filenames: list[str], prefix: str) -> int:
    return next((i + 1 for i, f in enumerate(filenames) if f.startswith(prefix)), 0)


def best_rank(filenames: list[str], want: list[str]) -> int:
    """多个期望里排得最靠前的那个（0=一个都没召回）。"""
    r = [rank_of(filenames, w) for w in want]
    r = [x for x in r if x > 0]
    return min(r) if r else 0


# ---------------------------------------------------------------- 用例加载

def load_cases() -> list[dict]:
    if not CASES.exists():
        sys.exit(
            f"找不到用例文件：{CASES}\n"
            "先在那里出题（字段 group/text/want/note），脚本不再内置题目、也不会静默用旧的。"
        )
    data = json.loads(CASES.read_text(encoding="utf-8"))
    cases = data["cases"] if isinstance(data, dict) else data
    bad = [c for c in cases if not c.get("group") or not c.get("text") or "want" not in c]
    if bad:
        sys.exit(f"用例文件里有 {len(bad)} 条缺字段（需要 group/text/want）：{bad[:2]}")
    if OPTS["only"]:
        hit = [c for c in cases if any(s in c["text"] for s in OPTS["only"])]
        if not hit:
            sys.exit(f"--only {OPTS['only']} 在这 {len(cases)} 条里没匹配到任何题，"
                     "先核对题面写法（口径不变，不会静默跑全量）")
        print(f"只跑 {len(hit)}/{len(cases)} 条："
              + "、".join(f"「{c['text']}」" for c in hit))
    return hit if OPTS["only"] else cases


# ---------------------------------------------------------------- 跑一条

def run(text: str, engine: str) -> dict:
    t0 = time.time()
    res = query.search(text, limit=LIMIT, engine=engine)
    res["_ms"] = int((time.time() - t0) * 1000)
    return res


def people_audit(res: dict) -> list[str]:
    # 只审计真正的“人数计数”硬条件：这类条件标签固定是“人数：恰好 N 人”。
    # 不能用“个人”当关键词——模型把 must 写成“人|人物|一个人”是“要有人出镜”的内容
    # 硬条件，不是把人数当门禁，早期用“个人”会把这种内容条件误报成人数硬条件。
    return [f"把人数写成硬条件：{c['label']}"
            for c in res["conds"] if c["hard"] and c["label"].startswith("人数")]


def conds_text(res: dict) -> str:
    on = [c for c in res["conds"] if not c["ignored"]]
    return "、".join((("硬·" if c["hard"] else "软·") + c["label"]) for c in on) or "（无生效条件）"


def eval_case(case: dict, want_recs: dict) -> dict:
    want = case["want"]
    text = case["text"]
    group = case["group"]
    rounds = []
    people_viol: list[str] = []
    for _ in range(ROUNDS):
        r = run(text, "qwen")
        fns = [it["filename"] for it in r["items"]]
        rank = best_rank(fns, want) if want else 0
        for m in people_audit(r):
            people_viol.append(m)
        rounds.append({
            "engine": r["engine"], "engine_note": r["engine_note"],
            "total": r["total"], "relaxed": r["relaxed"],
            "rank": rank, "top1": rank == 1, "top5": 0 < rank <= TOPK,
            "hit_wants": {w: rank_of(fns, w) for w in want},
            "top5fns": fns[:TOPK], "conds": conds_text(r), "ms": r["_ms"],
            # 把模型原话存下来：离线复现时直接重放这份意图，不再求模型
            "intent": r.get("intent") or {},
        })
    rules = run(text, "rules")

    totals = [x["total"] for x in rounds]
    fallen = [x for x in rounds if x["engine"] != "qwen"]
    any_hit = any(x["top5"] for x in rounds)
    all_hit = all(x["top5"] for x in rounds)
    fp_rounds = sum(1 for x in rounds if x["total"] > 0)      # C：有返回=误报的轮数

    overlap = 0
    for w in want:
        rec = want_recs.get(w)
        if rec:
            overlap = max(overlap, lcs(text, hay(rec)))
    openbook = overlap >= 4

    # B 召回：某一轮的完整返回结果里出现过该期望就算召回到
    recalled = sum(1 for w in want if any(r["hit_wants"].get(w, 0) > 0 for r in rounds))

    return {
        "group": group, "text": text, "want": want, "note": case.get("note", ""),
        "rounds": rounds,
        "rules": {"engine": rules["engine"], "total": rules["total"],
                  "rank": best_rank([i["filename"] for i in rules["items"]], want) if want else 0},
        "totals": totals, "total_median": statistics.median(totals),
        "ranks": [r["rank"] for r in rounds],
        "fallen": len(fallen), "any_hit": any_hit, "all_hit": all_hit,
        "fp_rounds": fp_rounds, "overlap": overlap, "openbook": openbook,
        "want_total": len(want), "recalled": recalled, "people_viol": people_viol,
    }


# ---------------------------------------------------------------- 汇总 / 门禁

def summarize(evs: list[dict]) -> tuple[dict, list[str]]:
    """返回 (headline 指标, 门禁失败项列表)。分母都先剔除开卷题。"""
    def scored(group):
        return [e for e in evs if e["group"] == group and not e["openbook"]]

    A = scored("A")
    A_all_hit = sum(1 for e in A if e["all_hit"])
    A_any_hit = sum(1 for e in A if e["any_hit"])
    A_rate = A_all_hit / len(A) if A else 0
    A_total_ok = [e for e in A if e["total_median"] <= TOO_MANY_A]

    B = scored("B")
    B_exp = sum(e["want_total"] for e in B)
    B_rec = sum(e["recalled"] for e in B)
    B_rate = B_rec / B_exp if B_exp else 0

    C = scored("C")
    C_fp = [e for e in C if e["fp_rounds"] > 0]

    DE = [e for e in evs if e["group"] in ("D", "E") and not e["openbook"]]
    DE_fall = [e for e in DE if e["fallen"] > 0]

    hit_cases = [e for e in evs if e["want"] and not e["openbook"]]
    overall_all = sum(1 for e in hit_cases if e["all_hit"])
    overall_any = sum(1 for e in hit_cases if e["any_hit"])
    med_totals = [e["total_median"] for e in hit_cases]
    total_med = statistics.median(med_totals) if med_totals else 0

    gates: list[str] = []
    if not A or A_rate < 0.80:
        lo, hi = wilson(A_all_hit, len(A))
        gates.append(f"A 组全轮命中率 {A_rate:.0%}（{A_all_hit}/{len(A)}，95%CI {lo:.0%}~{hi:.0%}）< 80%")
    over = [e for e in A if e["total_median"] > TOO_MANY_A]
    if over:
        gates.append(f"A 组有 {len(over)} 条 total 中位数 > {TOO_MANY_A}：" +
                     "、".join(f"「{e['text']}」{e['total_median']:.0f}" for e in over))
    if B_exp and B_rate < 0.80:
        gates.append(f"B 组召回 {B_rate:.0%}（{B_rec}/{B_exp}）< 80%")
    if C_fp:
        gates.append(f"C 组出现 {len(C_fp)} 条误报（应为 0）：" +
                     "、".join(f"「{e['text']}」{e['fp_rounds']}/{ROUNDS} 轮有返回" for e in C_fp))
    if DE_fall:
        gates.append(f"D/E 组有 {len(DE_fall)} 条回落到本地规则（应全程 qwen，单列不计分）：" +
                    "、".join(f"「{e['text']}」" for e in DE_fall))

    headline = {
        "A_count": len(A), "A_all_hit": A_all_hit, "A_any_hit": A_any_hit, "A_rate": A_rate,
        "B_exp": B_exp, "B_recalled": B_rec, "B_rate": B_rate,
        "C_count": len(C), "C_false": len(C_fp),
        "DE_count": len(DE), "DE_fallback": len(DE_fall),
        "hit_cases": len(hit_cases),
        "overall_all_hit": overall_all, "overall_any_hit": overall_any,
        "A_ci": wilson(A_all_hit, len(A)),
        "overall_ci": wilson(overall_all, len(hit_cases)),
        "total_median": total_med,
        "openbook": [e["text"] for e in evs if e["openbook"]],
    }
    return headline, gates


# ---------------------------------------------------------------- 报告

def main() -> None:
    cases = load_cases()
    photos = {r["filename"]: r for r in db._rows("SELECT * FROM photos")}
    stems = {fn.rsplit(".", 1)[0]: r for fn, r in photos.items()}

    def want_recs_for(want: list[str]) -> dict:
        return {w: next((v for k, v in stems.items() if k.startswith(w)), None) for w in want}

    cfg = llm.load()
    missing = sorted({w for c in cases for w in c["want"]
                      if not any(k.startswith(w) for k in stems)})

    evs = [eval_case(c, want_recs_for(c["want"])) for c in cases]
    headline, gates = summarize(evs)

    # ---- 回归护栏
    guard: list[tuple[str, bool, str]] = []
    g19 = [run(GUARD_2019, "qwen") for _ in range(ROUNDS)]
    ok19 = all(r["total"] == 0 for r in g19)
    guard.append((f"「{GUARD_2019}」total=0", ok19,
                  "、".join(str(r["total"]) for r in g19)))
    gn = run(GUARD_NOLISTEN, "qwen")
    okn = (not gn["conds"]) and ("没找到能对应相册的线索" in (gn["note"] or ""))
    guard.append((f"「{GUARD_NOLISTEN}」conds 为空且给没听懂提示", okn,
                  f"conds={len(gn['conds'])} note={gn['note'][:24] or '无'}"))
    viol = [f"「{e['text']}」{m}" for e in evs for m in e["people_viol"]]
    guard.append(("任何用例都不把人数当硬条件", not viol,
                  "全部只软加权 ✔" if not viol else "；".join(viol)))
    gates += [f"回归护栏不过：{name}（{detail}）" for name, ok, detail in guard if not ok]

    verdict = "不合格" if gates else "合格"

    L: list[str] = []
    L.append("查找功能 · 闭卷评测报告（可判涨跌版）")
    L.append(f"生成时间：{dt.datetime.now():%Y-%m-%d %H:%M}　"
             f"模型：{cfg['text_model']}（{'已启用' if cfg['enabled'] else '未启用'}）　"
             f"库内照片：{len(photos)} 张　用例：{len(cases)} 条　每条 qwen×{ROUNDS}+规则×1")
    L.append(f"通过门禁：【{verdict}】" + ("" if not gates else f"　{len(gates)} 项未达标"))
    for g in gates:
        L.append(f"　✗ {g}")
    L.append("")

    # ---- 三个汇总数 + 基线对比
    L.append("════════ 本轮汇总（分母已剔除开卷题）════════")
    olo, ohi = headline["overall_ci"]
    alo, ahi = headline["A_ci"]
    L.append(pad(f"全轮命中率（{ROUNDS} 轮全中）", 24) + f"{headline['overall_all_hit']}/{headline['hit_cases']}"
             + f"　95%CI {olo:.0%}~{ohi:.0%}（±{(ohi - olo) / 2:.0%}）")
    L.append(pad("　其中 A 组", 24) + f"{headline['A_all_hit']}/{headline['A_count']} = {headline['A_rate']:.0%}"
             + f"　95%CI {alo:.0%}~{ahi:.0%}（±{(ahi - alo) / 2:.0%}）")
    L.append(pad("至少一次命中率", 24) + f"{headline['overall_any_hit']}/{headline['hit_cases']}")
    L.append(pad("total 中位数（全命中题）", 24) + f"{headline['total_median']:.0f}")
    L.append(pad("B 组召回", 24) + f"{headline['B_recalled']}/{headline['B_exp']} = {headline['B_rate']:.0%}")
    L.append(pad("C 组误报", 24) + f"{headline['C_false']}/{headline['C_count']} 条")
    L.append(pad("D/E 回落", 24) + f"{headline['DE_fallback']}/{headline['DE_count']} 条")
    L.append("")
    L.append(f"—— {BASELINE['text']} ——")
    L.append(f"　A top-1 {BASELINE['A_top1']}　A top-5 {BASELINE['A_top5']}　"
             f"单句 total 最大 {BASELINE['single_total_max']}　C 误报 {BASELINE['C_false']}")
    L.append(f"　（本轮：用例 {len(cases)} 条、每条跑 {ROUNDS} 轮、反例 {headline['C_count']} 条，"
             "命中率附 Wilson 95%CI；与旧 13 条基线口径不同，只比趋势不比绝对值）")
    if headline["openbook"]:
        L.append(f"　开卷·不计分剔除 {len(headline['openbook'])} 条：" +
                 "、".join(f"「{t}」" for t in headline["openbook"]))
    if missing:
        L.append(f"　⚠ 用例引用了库里不存在的期望文件名：{missing}")
    L.append("")

    # ---- 排序在抖清单
    shaky = [e for e in evs if e["want"] and not e["openbook"]
            and e["any_hit"] and not e["all_hit"]]
    L.append(f"════════ 排序在抖（至少一次命中 但 没做到 {ROUNDS} 轮全中 → 模型给词不稳，非代码问题）════════")
    if shaky:
        for e in shaky:
            marks = "".join("✓" if r["top5"] else "✗" for r in e["rounds"])
            L.append(f"　[{e['group']}] 「{e['text']}」　{ROUNDS} 轮命中 {marks}　"
                     f"排名 {[r['rank'] or '未召回' for r in e['rounds']]}　"
                     f"total {e['totals']}")
    else:
        L.append(f"　没有：所有命中题 {ROUNDS} 轮都稳定。")
    L.append("")

    # ---- 逐条明细
    L.append("════════ 逐条明细 ════")
    for e in evs:
        L.append("")
        tag = "开卷·不计分" if e["openbook"] else ("回落" if e["fallen"] else "")
        L.append(f"[{e['group']}] {e['text']}　{tag}")
        L.append(f"　期望：{'、'.join(e['want']) or '（库里没有这东西，应 0 命中）'}　note：{e['note']}")
        for i, r in enumerate(e["rounds"], 1):
            extra = ""
            if e["group"] == "C":
                extra = "误报" if r["total"] > 0 else "干净"
            elif e["want"]:
                extra = f"top-1={'中' if r['top1'] else '不中'} top-5={'中' if r['top5'] else '不中'} 排名={r['rank'] or '未召回'}"
            L.append(f"　第{i}轮 {r['engine']} {r['ms']}ms total={r['total']}　{extra}"
                     + (f"　回落←{r['engine_note']}" if r["engine"] != "qwen" else "")
                     + (f"　放宽={r['relaxed']}" if r["relaxed"] else ""))
            L.append(f"　　 条件：{r['conds']}")
            L.append(f"　　 top-5：{r['top5fns'] or '（空）'}")
        L.append(f"　→ 至少一次命中={e['any_hit']}　{ROUNDS} 轮全命中={e['all_hit']}　"
                 f"total 中位数={e['total_median']:.0f}")
        if e["want"]:
            dist = "/".join(str(r["rank"]) if r["rank"] else "·" for r in e["rounds"])
            L.append(f"　排名分布（·＝未召回）：{dist}")
        L.append(f"　与库内原话最长重合：{e['overlap']} 字"
                 + ("　⚠ 疑似开卷，已从分母剔除" if e["openbook"] else "（闭卷）"))
        rr = e["rules"]
        L.append(f"　规则版对照：engine={rr['engine']} total={rr['total']} "
                 f"期望排名={rr['rank'] or '未召回'}")

    # ---- 每题排名分布（跨 {ROUNDS} 轮，一行一题，方便看整片稳不稳）
    L.append("")
    L.append(f"════════ 每题排名分布（{ROUNDS} 轮，·＝未召回，数字＝期望照片最好名次）════════")
    for e in evs:
        if not e["want"] or e["openbook"]:
            continue
        dist = "/".join(str(r["rank"]) if r["rank"] else "·" for r in e["rounds"])
        hitn = sum(1 for r in e["rounds"] if r["top5"])
        L.append(f"　[{e['group']}] {pad(e['text'], 22)} {dist}　命中 {hitn}/{ROUNDS}")

    # ---- 回落 / 问题清单
    L.append("")
    L.append("════════ 回落到本地规则（D/E 单列不计分）════════")
    fell = [e for e in evs if e["fallen"]]
    L.append(f"共 {len(fell)} 条有回落" if fell else "全部用例都由模型理解，成绩可信")
    for e in fell:
        L.append(f"　[{e['group']}] 「{e['text']}」 {ROUNDS} 轮中 {e['fallen']} 轮回落")

    REPORT.write_text("\n".join(L) + "\n", encoding="utf-8")
    write_raw(evs, headline, cfg, len(photos))

    print(f"报告：{REPORT}\n原始：{RAW}")
    print(f"{ROUNDS} 轮全命中 {headline['overall_all_hit']}/{headline['hit_cases']}　"
          f"至少一次命中 {headline['overall_any_hit']}/{headline['hit_cases']}　"
          f"total 中位数 {headline['total_median']:.0f}")
    print(f"排序在抖 {len(shaky)} 条　门禁：{verdict}")


def write_raw(evs: list[dict], headline: dict, cfg: dict, n_photos: int) -> None:
    meta = {"ts": f"{dt.datetime.now():%Y-%m-%d %H:%M}", "model": cfg["text_model"],
            "rounds": ROUNDS, "cases": len(evs), "photos": n_photos, "headline": headline}
    prev_hist: list = []
    if RAW.exists():
        try:
            prev = json.loads(RAW.read_text(encoding="utf-8"))
            prev_hist = prev.get("history", [])
            if prev.get("cases"):
                prev_hist = prev_hist + [{"meta": prev.get("meta"), "cases": prev["cases"]}]
        except Exception:
            pass
    out = {"meta": meta, "cases": evs, "history": prev_hist[-5:]}
    RAW.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
