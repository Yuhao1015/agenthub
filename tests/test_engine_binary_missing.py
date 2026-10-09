"""main.py 不在了要说人话（2026-08-31，用户实际踩到）。

真实故障：serve.py 是两周前启动的，import 时用 __file__ 把 main.py 的
绝对路径固化成了 ~/Downloads/agent-hub/main.py。用户后来把仓库挪走，
那个路径不存在了 —— Python「can't open file」退出码 2，看门狗只能补一句
「engine exited with code 2 before run_finished」。

用户看到的是「引擎异常退出（exited with code 2）……可以到设置页做一次
重新体检」，于是去体检，结果一切正常 —— 因为体检查的是 claude/dsh 这些
CLI 装没装，跟引擎自己的文件路径毫无关系。**建议指错了方向。**

这个条件是可以精确检测的：spawn 之前 os.path.exists(MAIN_PATH) 一查便知。
能精确检测的事就不该退化成通用崩溃。

Run: python3 tests/test_engine_binary_missing.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import serve
from test_gate_serve import authorize_local_agents

_GLOBALS = ("PORT", "MATRIX", "BOARD_PATH", "EVENTS_PATH", "WORKDIR", "DATA_DIR",
            "PROJECT", "RUNTIME_DIR", "RUNS_DIR", "INDEX_PATH", "CONFIG_PATH",
            "MERGED_MATRIX_PATH", "PREFLIGHT_PATH", "CREDENTIALS_PATH",
            "SCHEDULES_PATH", "MAIN_PATH", "_current", "_run_meta")


def _snap():
    return {"_runs": list(serve._runs), "_run_meta": dict(serve._run_meta),
            **{k: getattr(serve, k) for k in _GLOBALS}}


def _restore(s):
    for k, v in s.items():
        if k == "_runs":
            serve._runs = list(v)
        elif k == "_run_meta":
            serve._run_meta = dict(v)
        else:
            setattr(serve, k, v)


def _events(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def test_missing_main_py_is_named_precisely_not_a_generic_crash():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_nomain_")
    try:
        serve._configure(tmp, "default", 8787)
        authorize_local_agents()
        serve.MAIN_PATH = os.path.join(tmp, "definitely-not-here", "main.py")
        try:
            serve._spawn_main(["--goal=x"])
            raise AssertionError("main.py 不存在时必须抛出，而不是静默起一个必死的子进程")
        except OSError as e:
            msg = str(e)
        # 错误里要点名到底缺什么文件
        assert serve.MAIN_PATH in msg, f"没说清楚缺哪个文件: {msg}"
        # 而且要给出真正管用的建议：重启，不是体检
        assert "重启" in msg, f"没给出可执行的建议: {msg}"

        evts = _events(serve.EVENTS_PATH)
        crash = [e for e in evts if e["type"] == "run_crashed"]
        assert crash, "必须留下 run_crashed，否则 UI 永远显示「运行中」"
        assert serve.MAIN_PATH in crash[-1]["error"], crash[-1]
        # 不能**推荐**体检（体检查的是 CLI 装没装，跟引擎文件路径无关）；
        # 主动说明「做体检没有用」是好的，所以盯的是推荐语而不是「体检」二字
        assert "重新体检" not in crash[-1]["error"], \
            f"仍在建议重新体检，那会把人引向错误方向: {crash[-1]['error']}"
    finally:
        _restore(s); shutil.rmtree(tmp, ignore_errors=True)


def test_normal_spawn_path_untouched():
    """回归：main.py 在的时候，一个字都不该变。"""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_hasmain_")
    captured = {}

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    # 必须先 _configure 再 patch：_configure() 里的 git 探测走
    # subprocess.run()，而它内部用的就是 Popen —— 先 patch 会把它一起打坏
    # （tests/test_settings_and_watchdog.py 的 spawn-failure 用例有同样的注释）
    serve._configure(tmp, "default", 8787)
    authorize_local_agents()
    orig = serve.subprocess.Popen
    serve.subprocess.Popen = lambda argv, **kw: (captured.setdefault("argv", argv),
                                                  FakeProc())[1]
    try:
        pid = serve._spawn_main(["--goal=x"])
        assert pid == 4242
        assert captured["argv"][1] == serve.MAIN_PATH
    finally:
        serve.subprocess.Popen = orig
        _restore(s); shutil.rmtree(tmp, ignore_errors=True)


def test_empty_goal_is_named_precisely_not_exit_code_2():
    """--goal= 是 argparse 层面的错，main.py 在发出任何事件之前就 exit 2。

    真实故障（2026-08-31 12:11，run 26d78c24）：events.jsonl 里只有一行
    run_crashed「engine exited with code 2 before run_finished」，没有
    run_started、没有 plan_created。用户看到的是一次凭空失败的运行，而
    真正的原因——引擎压根没拿到目标——一个字都没写出来。

    跟 MAIN_PATH 那条是同一类：能精确检测的条件不该退化成通用崩溃。
    """
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_nogoal_")
    try:
        serve._configure(tmp, "default", 8787)
        authorize_local_agents()
        before = list(serve._runs)
        try:
            serve._spawn_main(["--goal="])
            raise AssertionError("空目标必须抛出，而不是起一个必死的子进程")
        except OSError as e:
            msg = str(e)
        assert "目标" in msg, f"没说清楚缺的是什么: {msg}"
        assert "体检" in msg, f"要主动说明体检看不出问题: {msg}"
        # 拦在建 run 目录之前：不留下一次空壳失败运行
        assert list(serve._runs) == before, "不该为一次注定失败的 spawn 建运行记录"
        assert not os.path.isdir(serve.RUNS_DIR) or not os.listdir(serve.RUNS_DIR), \
            "不该留下空的 run 目录"
    finally:
        _restore(s); shutil.rmtree(tmp, ignore_errors=True)


def test_whitespace_only_goal_counts_as_empty():
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_wsgoal_")
    try:
        serve._configure(tmp, "default", 8787)
        authorize_local_agents()
        try:
            serve._spawn_main(["--goal=   \n"])
            raise AssertionError("只有空白的目标同样会让 main.py exit 2")
        except OSError:
            pass
    finally:
        _restore(s); shutil.rmtree(tmp, ignore_errors=True)


def test_resume_and_plan_need_no_goal():
    """回归护栏：--resume 从黑板重建 DAG，--plan 直接给计划，两者都没有
    --goal，绝不能被新守卫拦下。"""
    assert serve._engine_target_missing(["--resume", "--gate-mode=auto"]) == ""
    assert serve._engine_target_missing(["--plan=/tmp/p.json"]) == ""
    assert serve._engine_target_missing(["--goal=做个看板"]) == ""
    assert serve._engine_target_missing(["--goal=", "--plan="]) != ""
    assert serve._engine_target_missing(["--gate-mode=auto"]) != ""


def test_chat_confirm_on_an_empty_proposal_goal_reports_instead_of_crashing():
    """真正可达的那条路：会话 schema 只校验 proposal["goal"] 是不是字符串，
    不校验非空，于是空目标能一路走到 spawn。"""
    s = _snap()
    tmp = tempfile.mkdtemp(prefix="ah_confirm_")
    try:
        serve._configure(tmp, "default", 8787)
        authorize_local_agents()
        spawned = []
        orig = serve.subprocess.Popen
        serve.subprocess.Popen = lambda argv, **kw: spawned.append(argv)
        try:
            serve._spawn_manager_main_if_idle(["--goal=", "--constraints=x"])
            raise AssertionError("必须抛 OSError（调用方已经在 catch 它）")
        except OSError as e:
            assert "目标" in str(e)
        finally:
            serve.subprocess.Popen = orig
        assert not spawned, "一个注定 exit 2 的子进程都不该被起起来"
    finally:
        _restore(s); shutil.rmtree(tmp, ignore_errors=True)



if __name__ == "__main__":
    for fn in sorted(k for k in list(globals()) if k.startswith("test_")):
        print(f"== {fn} ==")
        globals()[fn]()
    print("\nAll engine-binary-missing tests passed.")
