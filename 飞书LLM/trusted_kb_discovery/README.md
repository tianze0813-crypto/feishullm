# 可信知识库发现器

这个目录用于批量发现适合沉淀为“找人/找入口/找归口”可信知识库的高价值文档。

## 文件说明

- `discover.py`
  - 批量调用现有飞书搜索与正文读取接口
  - 合并重复命中
  - 根据重复度、标题信号、正文信号做排序
  - 输出 `JSON + CSV`
- `queries_full.json`
  - 默认的事务类别与查询词全集
- `output/`
  - 运行脚本后的结果目录

## 运行方式

在项目根目录执行：

```powershell
python .\trusted_kb_discovery\discover.py --open-id ou_xxx
```

或使用 PowerShell 版本：

```powershell
powershell -ExecutionPolicy Bypass -File ".\trusted_kb_discovery\discover.ps1" -OpenId ou_xxx
```

## 常用参数

```powershell
python .\trusted_kb_discovery\discover.py `
  --open-id ou_xxx `
  --docs-page-size 8 `
  --wiki-page-size 8 `
  --raw-top-n 120 `
  --top-n 100
```

## 分批运行

如果一次性跑全部 query 容易触发飞书限流，可按批次执行。

Python 示例：

```powershell
python .\trusted_kb_discovery\discover.py `
  --open-id ou_xxx `
  --batch-size 12 `
  --batch-index 1 `
  --query-sleep-seconds 1.5
```

PowerShell 示例：

```powershell
powershell -ExecutionPolicy Bypass -File ".\trusted_kb_discovery\discover.ps1" `
  -OpenId ou_xxx `
  -BatchSize 12 `
  -BatchIndex 1 `
  -QuerySleepMs 1500
```

说明：

- `batch-size`
  - 每批处理多少个 query
- `batch-index`
  - 当前执行第几批，从 `1` 开始
- `query-sleep-seconds / QuerySleepMs`
  - 每个 query 之间的等待时间，用来降低触发 `99991400` 的概率
- 输出文件会自动带批次后缀
  - 例如 `trusted_find_person_candidates.batch-01-of-09.csv`

## 输出内容

- `trusted_find_person_candidates.json`
  - 完整候选信息、分数、命中类别、命中 query、正文预览
- `trusted_find_person_candidates.csv`
  - 适合人工筛选高价值文档

## 推荐筛选方式

优先保留这些文档：

- 重复命中多个事务类别
- 标题包含 `负责人 / 归口 / 对接人 / SOP / 流程 / 指引 / FAQ / 制度`
- 正文包含 `提交工单 / 申请入口 / 工作台 / 请联系 / 操作步骤`
- 来自正式知识库或权威文档目录

优先剔除这些文档：

- 周报、月报、纪要、培训分享、草稿、测试文档
- 只有标题命中、正文没有有效信号的文档

## 后续建议

第一轮建议先人工筛出 30-50 篇高价值文档，建立可信知识库白名单。
后续再把白名单落到 SQLite，接入 `find_person` 优先检索。
