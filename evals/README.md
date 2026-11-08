# 评测套件（pytest）

对**运行中的**香水 Agent 服务发起真实 HTTP 请求，按评测集断言，输出按类别分组的报告。
不改被测接口的任何业务逻辑，也不 mock 任何东西。

## 一条命令跑完

```bash
# 1. 用评测模式启动被测服务（必须带 EVAL_HOOKS=1，否则注入不了初始画像）
EVAL_HOOKS=1 uvicorn app.main:app --port 8000

# 2. 另开一个终端，跑评测
pytest evals/ -v --split=tune
```

Windows PowerShell：

```powershell
$env:EVAL_HOOKS='1'; uvicorn app.main:app --port 8000
# 另开终端
pytest evals/ -v --split=tune
```

## 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--split` | `all` | `all` / `tune` / `holdout`，跑哪一部分 |
| `--repeat` | `1` | 每条用例重复执行 N 次，输出 `E04: 3/3` 形式看稳定性 |
| `--base-url` | `http://127.0.0.1:8000` | 被测服务地址 |
| `--dataset` | `data/eval_cases.json` | 评测集路径 |

常用组合：

```bash
pytest evals/ -v --split=tune              # 调优集，19 条
pytest evals/ -v --split=holdout           # 留出集，9 条
pytest evals/ -q --repeat=3                # 全量跑 3 轮，看稳定性
```

## 输出长什么样

```
类别                  tune
普通推荐                3/4
否定表达                1/4
相对强度                2/3
多轮修改偏好              2/4
禁忌保留与误拦截            4/4
--------------------------------
合计                  12/19
```

每条失败只打印**一段**可诊断文本，不刷堆栈：

```
用例 B04｜否定表达｜tune
  输入      : 我不太喜欢太浓的味道，淡一点最好
  前置画像  : （新用户）
  实际画像  : {...}
  问题      :
    - intensity 期望 '淡' 实际 '浓'
```

## 结果留档

每次运行写两个文件到 `evals/results/`：

- `{timestamp}_{version}.json` —— 机读，含数据集版本、commit、每条用例通过与否、失败原因
- `{timestamp}_{version}.md` —— 人读，表格 + 失败详情

用来对比"改之前 vs 改之后"。

## 为什么有 `/_test/profile` 这个接口

评测集里大量用例依赖"测前已有画像"。比如 C01「比上次淡一点点」——
不先把 `intensity=中` 注入进去，这条用例根本测不出相对调整能力。

这个钩子只做两件事：写入/清除某个用户的 profile，不碰保密逻辑、不返回配方。三道保险：

1. 必须 `EVAL_HOOKS=1` 启动才生效，默认返回 404
2. 只接受 `eval_` 前缀的 `user_id`
3. `include_in_schema=False`，不出现在 `/docs` 里

## 隔离机制

每条用例：

```
清理 eval_{id} 画像 → 注入 profile_before → 发 input → 断言 → 清理 eval_{id} 画像
```

用独立 `user_id` 而不是共用，是因为禁忌类用例会互相污染——
B01 说"讨厌玫瑰"，如果和 B02 共用一个画像，B02 的推荐就会被 B01 的禁忌影响。

## 断言规则

| 断言 | 规则 | 为什么 |
|---|---|---|
| 画像 | **子集匹配**，列表字段要求期望项都出现，多出来的不扣分 | LLM 输出不唯一，全等比对会大量假失败 |
| 禁止项 | 扫卡片的 `name / family / notes / desc / tags / audience` 全部文本 | 只看标签会漏掉写在描述里的词 |
| 拦截 | 推荐必须为空 **且** 有拒绝话术 | 空推荐但也不说话，不算合格拦截 |

`expect_block=true` 时只查拦截，不再查画像——被拦下的请求本来就不该产生画像。

## 加新用例

直接往 `data/eval_cases.json` 的 `cases` 数组里加，`id` 唯一即可：

```json
{
  "id": "F01",
  "split": "holdout",
  "category": "普通推荐",
  "profile_before": {"scent_family": "木质"},
  "input": "想要一款适合秋天的",
  "expected_profile": {"occasion": "秋冬"},
  "must_not": ["柑橘"],
  "expect_block": false,
  "notes": "留出集：季节限定"
}
```

改完记得同步 `meta.total` 和 `meta.split`——评测会校验并在不一致时打警告。

## 已知的数据集问题

`meta` 声明 `total=30 / tune=20 / holdout=10`，但文件里的实际用例是 **28 条**（tune 19 + holdout 9）。
所以 `--split=tune` 实际跑 **19 条**，不是验收标准里写的 20 条。评测会在结尾打警告，
需要在补题或修正 meta 之间二选一。
