"""pytest 公共垫层：让测试在**没有真实凭证的机器**上（CI、fork 后）也能全绿。

为什么需要它：providers.py 在 import 时就按「配了 Key 的供应商」建客户端与模型梯队
（AI_CLIENTS / MODEL_CHAINS...），config 里所有 Key 又取决于 .env / 环境变量。
在配置齐全的开发机上跑没问题；在 fresh clone 上跑，8 个用例会 KeyError。
所以这里在 import config 之前，把**缺失的**供应商 Key 垫成哑值 —— 结构照常建立，
纯结构性测试全绿；真要打网络的少量用例（见 test_failover.py）用 keys_are_real()
自查，哑 Key 语境下跳过，行为与「没配 Key」一致。
"""
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_TESTS_DIR)
for _p in (_TESTS_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

HERMETIC_PREFIX = "sk-hermetic-dummy"


def _env_has_real_key(env_file, key):
    """.env 里这个 Key 有没有被填上真值（非空、非注释）？"""
    try:
        with open(env_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'").strip()
                    return bool(val)
    except OSError:
        pass
    return False


# 只在确实没有真值时垫哑值：开发机上 .env 的真 Key 仍然生效（打网络的用例照跑）。
_repo_env = os.path.join(_REPO_ROOT, ".env")
for _name in ("GEMINI_API_KEY", "ZHIPU_API_KEY"):
    if not os.environ.get(_name) and not _env_has_real_key(_repo_env, _name):
        os.environ.setdefault(_name, f"{HERMETIC_PREFIX}-{_name.lower()}")

# 测试里一部分用例假定「主 gemini + 备 zhipu」的双供应商链（LastResortTest /
# StructuralFailureTest 直接对两家的状态表动手脚）。fresh clone 的 .env 默认是
# AI_PROVIDER=zhipu 单链，结构对不上 —— 这里把测试语境补成与正常部署同形。
for _name in ("AI_PROVIDER", "FALLBACK_PROVIDER"):
    if not os.environ.get(_name) and not _env_has_real_key(_repo_env, _name):
        os.environ.setdefault(_name, "gemini" if _name == "AI_PROVIDER" else "zhipu")


def keys_are_real(provider=None):
    """该供应商有没有至少一个非哑 Key。

    结构性测试不需要调用它；**要真连供应商**的用例在 setUp 里自查一次，
    哑 Key 语境直接 skipTest —— 与「这台机器没配 Key」语义对齐。
    """
    import config
    keys = config.load_api_keys(provider) or []
    return any(not str(k).startswith(HERMETIC_PREFIX) for k in keys)
