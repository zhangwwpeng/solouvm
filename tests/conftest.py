import importlib.util

# 任务 3 完成前 solouvm/errors.py 还不存在；跳过它的测试，免得整轮 pytest 在收集阶段中断。
# errors.py 一建好，test_errors.py 会自动恢复收集。
collect_ignore = [] if importlib.util.find_spec("solouvm.errors") else ["test_errors.py"]
