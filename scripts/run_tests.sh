#!/usr/bin/env bash
# 跑本仓库的全部测试。
#
# 每个测试文件都在**独立进程**里跑：它们各自往 sys.modules 里塞假的 astrbot
# 模块来离线运行，同一个进程里跑两个会互相覆盖对方的桩
# （test_logic 先跑的话，pages_api 会绑定到它的 request 桩，
#  test_settings 的接口就再也读不到请求体）。
set -euo pipefail

cd "$(dirname "$0")/.."

PY=${PY:-python}
fail=0

echo "== 离线回归测试 =="
"$PY" -X utf8 tests/test_logic.py || fail=1

echo
echo "== 面板设置端到端测试 =="
"$PY" -X utf8 tests/test_settings.py || fail=1

echo
echo "== 面板前端测试（需要 jsdom）=="
for f in tests/panel_load.test.js tests/panel_no_bridge.test.js \
         tests/panel.test.js tests/style_consistency.test.js; do
  if [ -f "$f" ]; then
    node "$f" || fail=1
  fi
done

echo
if [ "$fail" -ne 0 ]; then
  echo "有测试未通过"
  exit 1
fi
echo "全部通过"
