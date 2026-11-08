# 交付：香水 Agent 评测套件

需求来源：`用DSH写评测代码的提示词.md` + `香水Agent评测集_v1.json`。

## 实现清单

| 任务 | 状态 | 产物 / 说明 |
|---|---|---|
| E1 fixtures 与服务检查 | 完成 | `evals/conftest.py`：`--split` / `--repeat` / `--base-url` / `--dataset` 四个参数；服务不可达或钩子未开时给出**可直接照抄的启动命令**并 exit 2；`EvalClient` 纯标准库实现，不引入新依赖 |
| E2 参数化测试主体 | 完成 | `evals/test_eval.py`：一条用例 = 一个 pytest item，id 即 `case_id`；每条用例清理 → 注入 → 请求 → 断言 → 回收，`user_id = eval_{id}` 严格隔离 |
| E3 三类断言 | 完成 | `evals/assertions.py`：`check_profile()` 子集匹配、`check_must_not()` 扫全部可见文本、`check_block()` 空推荐 + 有拒绝话术；`expect_block=true` 时短路不查画像 |
| E4 统计与报告 | 完成 | `evals/report.py`：按 category × split 出表；`--repeat` 输每条通过次数并区分「稳定失败」与「时好时坏」；结果写 `evals/results/{timestamp}_{version}.json` 与 `.md`，含数据集版本与 commit |
| E5 一键说明 | 完成 | `evals/README.md` |
| E6 测试钩子 | 完成 | `app/main.py` 新增 `/_test/profile`（写）、`DELETE /_test/profile/{uid}`（清）、`/_test/health`（探活）。三道保险：`EVAL_HOOKS=1` 才生效、只认 `eval_` 前缀、`include_in_schema=False`。**未改动任何业务接口实现** |

## 如何运行

```powershell
# 终端 1：以评测模式启动被测服务
cd D:\Deepseek\perfume-agent
$env:EVAL_HOOKS='1'; uvicorn app.main:app --port 8000

# 终端 2
pytest evals/ -v --split=tune
```

## 如何验证

```powershell
# 1. 分组统计（预期 19 条 tune）
pytest evals/ -q --split=tune

# 2. 留出集
pytest evals/ -q --split=holdout

# 3. 稳定性（每条跑 3 次）
pytest evals/ -q --repeat=3

# 4. 证明断言不是永远通过的假测试：篡改 must_not 后必须失败
python -c "import json,pathlib; p=pathlib.Path('data/eval_cases.json'); d=json.loads(p.read_text(encoding='utf-8')); [c.update(must_not=['清新']) for c in d['cases'] if c['id']=='A01']; pathlib.Path('data/_tmp_poison.json').write_text(json.dumps(d,ensure_ascii=False),encoding='utf-8')"
pytest evals/ -q --split=tune --dataset data/_tmp_poison.json -k A01
# 预期：1 failed —— A01 被「清新」命中三张卡片
```

## 自测结果

| 验收标准 | 结果 |
|---|---|
| `pytest evals/ -v --split=tune` 能跑通并输出分组表 | 通过（实际 19 条，**非 20 条**，见下） |
| 篡改 `must_not` 后测试必须失败 | 通过，A01 由 PASS 变 FAIL，且精确点名 3 张卡片 |
| 全流程不改动被测接口业务实现 | 通过，只新增 `/_test/*` 三个钩子，9 个业务接口零改动 |

- 通过：3/3 项验收
- 失败：0 项
- 未运行：1 项（原因：LLM 模式，环境无 `DEEPSEEK_API_KEY`，全部结果均在规则模式下取得）

### 本轮评测实测数字

```
类别                  tune        holdout
普通推荐              3/4         0/2
否定表达              1/4         0/2
相对强度              2/3         1/1
多轮修改偏好              2/4         1/2
禁忌保留与误拦截            4/4         2/2
--------------------------------------------
合计                  12/19       4/9
```

总计 **16/28 通过（57.1%）**。tune 12/19（63%），holdout 4/9（44%）。

## 评测暴露的问题（按类别看，这比总通过率有用得多）

**最强项**：禁忌保留与误拦截 6/6 —— 保密拦截没误伤正常需求，`E04`「星际漫步这个配方适合我吗」这种含"配方"但只是问适合度的句子没有被误拦。

**最弱项**：否定表达 **1/6**。12 条失败里 5 条集中在这里：

| 用例 | 输入 | 期望 | 实际 | 根因 |
|---|---|---|---|---|
| B02 | 别给我茉莉，闻着头晕 | taboos ⊇ [茉莉] | taboos 空 | 否定词表里没有「茉莉」这个靶子，且「别给我」不是识别前缀 |
| B03 | 不要甜的 | intensity=淡 | 空 | 「甜」被记进 taboos，但没有从"不要甜"推出"要淡" |
| B04 | 我不太喜欢太浓的味道，淡一点最好 | intensity=淡 | 浓 | 「浓」在强信号表里先命中，「不太喜欢」的否定语义被丢弃 |
| B05 | 柑橘类我有点受不了 | taboos ⊇ [柑橘] | 空 | 「受不了」不在否定前缀表 |
| B06 | 除了花香其他都能接受 | taboos ⊇ [花香] | 空 | 排除式表达完全没有覆盖 |

多条轮修改偏好里同样的病根：

| 用例 | 输入 | 期望 | 实际 | 根因 |
|---|---|---|---|---|
| D02 | 我对玫瑰也过敏 | taboos ⊇ [柑橘, 玫瑰] | [柑橘] | 「过敏」不是否定前缀；好消息是旧禁忌**确实保留了**，覆盖逻辑本身没坏 |
| D05 | 清新一点，但茉莉不行 | taboos ⊇ [茉莉] | 空 | 「X 不行」句式未覆盖 |

相对强度 2 条：

| 用例 | 输入 | 期望 | 实际 | 根因 |
|---|---|---|---|---|
| C02 | 再来点存在感，浓一些 | 中（淡→中） | 浓 | 相对词表只有「一点/稍微」，不认「一些」，直接跳档 |
| C04（holdout，通过） | 这个太冲了，收敛一点 | 中 | 中 | — 「收敛」需补进词表才不会靠运气 |

场景词表与评测口径不一致 4 条：

| 用例 | 输入 | 期望 | 实际 |
|---|---|---|---|
| A03 | 送女朋友结婚伴手礼 | occasion=婚礼伴手礼 | 婚礼（词表里「结婚」排在「伴手礼」前） |
| A06 | 要出席一个正式晚宴 | occasion=宴会 | 晚宴 |
| D03 | 这次是出差 | occasion=通勤 | 约会（没有「出差」这个词） |
| A05 | 夏天用，闻着凉快那种 | scent_family=清新 | 空（没有「凉快」） |

**结论**：这 12 条里约 10 条是"词表覆盖不足"这一类问题，靠补规则就能吃掉一半以上；剩下 B04 那种"否定语义 vs 强度词"的冲突，规则方式容易按下葫芦浮起瓢，是上 LLM 抽取的正当理由。

## 数据集问题（必须先处理）

`meta` 声明 `total=30 / tune=20 / holdout=10`，但 `cases` 数组实际只有 **28 条**（tune 19 + holdout 9）。

所以验收标准里"`--split=tune` 能跑通 20 条"这条**无法按字面满足**——不是评测代码的问题，是数据集缺 2 条（看起来是每个类别各缺 1 条 tune 和 1 条 holdout）。

评测会在每次运行结尾打警告：

```
数据集声明 total=30 {'tune': 20, 'holdout': 10}｜实际跑 total=28 {'tune': 19, 'holdout': 9}
  ⚠ 数据集 meta 与用例条数不一致（声明 30，实际 28），请核对 data/eval_cases.json
```

两条路：补齐 2 条用例，或把 meta 改成 28/19/9。**建议补题**——holdout 才 9 条，统计置信度偏弱。

## 未完成 / TODO

- **LLM 模式未评测**：原因：环境无 `DEEPSEEK_API_KEY`，全部结论来自规则模式。配 Key 后建议先跑 `--repeat=3` 看稳定性，规则模式和 LLM 模式的失败分布会很不相同（规则模式错在词表，LLM 模式更可能错在"把否定理解反"）。
- **C04 靠运气通过**：「这个太冲了，收敛一点」目前期望值恰好等于原值（浓→中 靠「一点」触发相对降档），并没有真正识别「太冲=嫌浓」。建议补一条更尖锐的用例。
- **并发隔离未验证**：`user_id` 按用例隔离，但两条用例并行执行（`-n` 插件）时没有额外保护，本次是串行跑的。
