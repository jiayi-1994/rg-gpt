"""定时触发 GitHub Actions register-accounts 工作流，批量注册账号写回 sub2api。

不在本地跑浏览器 —— 每隔一段时间用 gh CLI 触发一次 CI 工作流(40 job 并发，
每 job count_per_job 个号，按 JOB_INDEX 把 job 摊到下面的域名列表)。失败的 job
会因 account_creation_failed / 超时快速结束，不占用配额。

依赖：本机已装并登录 gh（`gh auth status` 能过）。纯标准库，无需 pip 安装。

用法：
    python schedule_register.py                # 每 60 分钟触发一次，循环
    python schedule_register.py --once         # 只触发一次，等结果后退出
    python schedule_register.py --interval-min 30 --count 2
    python schedule_register.py --no-wait      # 触发后不等结果，立刻进入下一轮等待

Windows 后台常驻：可用 `pythonw schedule_register.py` 或丢进“任务计划程序”。
也可以改用 GitHub 原生 cron（见文件末尾注释），就不用本机常驻。
"""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime

REPO = "jiayi-1994/rg-gpt"
WORKFLOW = "register.yml"

# 可注册的邮箱域名（CloudMail 后台已配）。job 会按 JOB_INDEX 轮询摊到这些域名。
DOMAINS = [
    "jiayi.dpdns.org",
    "jiayi.ggff.net",
    "edu.jiayi.ggff.net",
    "xjy.hidns.vip",
    "edu.xjy.hidns.vip",
    "sub.xjy.hidns.vip",
    "sub1.xjy.hidns.vip", "sub2.xjy.hidns.vip", "sub3.xjy.hidns.vip", "sub4.xjy.hidns.vip",
    "sub5.xjy.hidns.vip", "sub6.xjy.hidns.vip", "sub7.xjy.hidns.vip", "sub8.xjy.hidns.vip",
    "sub9.xjy.hidns.vip", "sub10.xjy.hidns.vip", "sub11.xjy.hidns.vip", "sub12.xjy.hidns.vip",
    "sub13.xjy.hidns.vip", "sub14.xjy.hidns.vip", "sub15.xjy.hidns.vip", "sub16.xjy.hidns.vip",
    "sub17.xjy.hidns.vip", "sub18.xjy.hidns.vip", "sub19.xjy.hidns.vip", "sub20.xjy.hidns.vip",
]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


def _gh(args: list[str]) -> tuple[int, str]:
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def latest_run_id() -> str | None:
    rc, out = _gh(["run", "list", "-R", REPO, "-w", WORKFLOW, "-L", "1",
                   "--json", "databaseId", "-q", ".[0].databaseId"])
    rid = out.strip()
    return rid if rc == 0 and rid.isdigit() else None


def dispatch(count: int) -> bool:
    domains_csv = ",".join(DOMAINS)
    rc, out = _gh(["workflow", "run", WORKFLOW, "-R", REPO,
                   "-f", f"count_per_job={count}", "-f", f"domains={domains_csv}"])
    if rc != 0:
        log(f"触发失败: {out.strip()[:200]}")
        return False
    log(f"已触发工作流 (count_per_job={count}, domains={len(DOMAINS)})")
    return True


def wait_for_run(run_id: str, poll_sec: int = 25, max_min: int = 25) -> None:
    deadline = time.time() + max_min * 60
    while time.time() < deadline:
        rc, out = _gh(["run", "view", run_id, "-R", REPO, "--json", "status,jobs"])
        if rc != 0:
            time.sleep(poll_sec)
            continue
        try:
            d = json.loads(out)
        except json.JSONDecodeError:
            time.sleep(poll_sec)
            continue
        jobs = d.get("jobs", [])
        done = [j for j in jobs if j.get("status") == "completed"]
        ok = sum(1 for j in done if j.get("conclusion") == "success")
        bad = sum(1 for j in done if j.get("conclusion") != "success")
        if d.get("status") == "completed":
            log(f"本轮完成: 成功 {ok} / 失败 {bad} (run {run_id})")
            return
        log(f"运行中: 成功 {ok} / 失败 {bad} / 总 {len(jobs)}")
        time.sleep(poll_sec)
    log(f"等待超时(>{max_min}min)，不再等待 run {run_id}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval-min", type=int, default=60, help="每轮间隔分钟(默认 60)")
    ap.add_argument("--count", type=int, default=2, help="count_per_job(默认 2)")
    ap.add_argument("--once", action="store_true", help="只触发一轮后退出")
    ap.add_argument("--no-wait", action="store_true", help="触发后不等结果")
    args = ap.parse_args()

    rc, _ = _gh(["auth", "status"])
    if rc != 0:
        log("gh 未登录，请先 `gh auth login`")
        sys.exit(1)

    log(f"调度启动: repo={REPO} interval={args.interval_min}min count={args.count} "
        f"domains={len(DOMAINS)} once={args.once}")

    while True:
        if dispatch(args.count) and not args.no_wait:
            time.sleep(8)
            rid = latest_run_id()
            if rid:
                wait_for_run(rid)
        if args.once:
            break
        log(f"下一轮 {args.interval_min} 分钟后…")
        time.sleep(max(1, args.interval_min) * 60)


if __name__ == "__main__":
    main()

# --- 备选：GitHub 原生定时(无需本机常驻) ---
# 在 .github/workflows/register.yml 的 on: 下加：
#   schedule:
#     - cron: "0 * * * *"   # 每小时(UTC)。cron 触发用 inputs 的 default 值。
# 注意 GitHub schedule 最快 ~5 分钟粒度，且仓库 60 天无活动会自动停用。
