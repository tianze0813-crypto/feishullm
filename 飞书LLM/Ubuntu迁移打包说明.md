# Ubuntu 迁移打包说明

## 1. 文档目的

本文档用于指导当前 `飞书LLM` 项目从 Windows 电脑整体打包，迁移到 Ubuntu 电脑后重新安装依赖、恢复配置并启动运行。

本文档重点解决以下问题：

- 打包时哪些文件必须带走
- 哪些文件不要带走
- Ubuntu 上需要安装什么
- 项目到 Ubuntu 后如何重新创建运行环境
- 如何避免 OAuth、会话、配置丢失

---

## 2. 迁移方式建议

本次建议采用“项目文件夹整体打包后复制到 Ubuntu”的方式迁移。

推荐流程：

1. 在当前 Windows 电脑整理项目目录
2. 删除不需要迁移的平台相关文件
3. 保留代码、配置和本地持久化数据
4. 打包为一个压缩文件
5. 下载或拷贝到 Ubuntu 电脑
6. 在 Ubuntu 上重新创建 Python 虚拟环境并安装依赖
7. 检查 `.env`、回调地址和授权数据
8. 启动并验证

---

## 3. 打包时必须保留的文件

以下内容建议保留并一起迁移：

### 3.1 项目源码

整个项目目录中的以下内容都建议保留：

- `api/`
- `core/`
- `feishu_client/`
- `llm/`
- `trusted_kb_discovery/`
- `utils/`
- `main.py`
- `config.py`
- `requirements.txt`
- `api.json`
- 各类 `.md` 方案文档

### 3.2 环境变量文件

必须保留：

- `.env`

说明：

- 项目配置由 `config.py` 从项目根目录的 `.env` 读取
- 如果 `.env` 丢失，项目虽然可能启动，但飞书配置、DeepSeek 配置、OAuth 配置都会失效

### 3.3 用户授权数据

如果你希望迁移后继续保留已有授权状态，必须保留：

- `.tokens/`

重点文件：

- `.tokens/user_tokens.json`

说明：

- 这里保存的是用户 OAuth token
- 如果不迁移这个目录，Ubuntu 上服务启动后用户通常需要重新授权

### 3.4 会话和上下文数据

如果你希望保留历史会话和话题上下文，建议保留：

- `.memory/`

重点文件：

- `.memory/conversations.db`

说明：

- 这个文件是本地 SQLite 会话库
- 不迁移也能运行，但历史会话、上下文、话题切换信息会丢失

### 3.5 可选保留内容

按需保留：

- `.logs/`
- `trusted_kb_discovery/output/`
- `项目文档.md`
- `企业级知识检索全量改造方案.md`
- `企业级知识检索全量执行清单.md`

说明：

- `.logs/` 主要用于排查历史问题
- `trusted_kb_discovery/output/` 如果你后续还要继续利用已有产物，建议一起迁移

---

## 4. 打包时不要带走的文件

以下内容不建议打包：

- `.venv/`
- `.venv_local/`
- `__pycache__/`
- `*.pyc`
- Windows 下生成的 `Lib/`
- Windows 下生成的 `Scripts/`
- `setup_venv.bat`

原因：

- 虚拟环境和 Python 依赖是平台相关的
- Windows 的虚拟环境复制到 Ubuntu 上通常不能直接用
- 到 Ubuntu 后应重新创建虚拟环境并重新安装依赖

---

## 5. 打包前整理建议

在 Windows 打包前，建议先确认目录结构。

推荐最终打包内容如下：

```text
飞书LLM/
├─ api/
├─ core/
├─ feishu_client/
├─ llm/
├─ trusted_kb_discovery/
├─ utils/
├─ .env
├─ .tokens/
├─ .memory/                  # 如果要保留历史会话则一起带
├─ .logs/                    # 可选
├─ main.py
├─ config.py
├─ requirements.txt
├─ 项目文档.md
├─ 企业级知识检索全量改造方案.md
└─ 企业级知识检索全量执行清单.md
```

如果你想减小包体积，可在打包前删除：

- `.venv/`
- `.venv_local/`
- `__pycache__/`
- 无用日志

---

## 6. 打包操作建议

### 6.1 最简单方式

直接将 `飞书LLM` 文件夹压缩为一个 zip 包。

例如：

- `飞书LLM-ubuntu-migrate.zip`

### 6.2 打包前自查清单

打包前建议确认以下项目：

- [ ] `.env` 已存在且内容正确
- [ ] `.tokens/` 是否需要保留
- [ ] `.memory/` 是否需要保留
- [ ] `.venv/` 和 `.venv_local/` 已删除或不纳入压缩包
- [ ] 项目代码是最新版本
- [ ] 文档和方案文件已保存

---

## 7. Ubuntu 上需要安装的系统依赖

先安装 Python 和基础工具：

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

可选检查：

```bash
python3 --version
pip3 --version
```

建议 Python 版本：

- Python 3.10 及以上

---

## 8. 把压缩包放到 Ubuntu 后的操作

假设你把压缩包放在 `~/Downloads`。

### 8.1 解压

```bash
cd ~/Downloads
unzip 飞书LLM-ubuntu-migrate.zip
```

如果你想把项目放到 `~/projects`：

```bash
mkdir -p ~/projects
mv ~/Downloads/飞书LLM ~/projects/
cd ~/projects/飞书LLM
```

### 8.2 检查关键文件是否存在

确认以下内容已经到位：

```bash
ls -la
```

重点检查：

- `.env`
- `.tokens/`
- `.memory/`
- `requirements.txt`
- `main.py`

如果 `.env` 不显示，执行：

```bash
ls -la
```

因为以 `.` 开头的文件默认可能不明显。

---

## 9. Ubuntu 上创建虚拟环境并安装依赖

进入项目目录后执行：

```bash
cd ~/projects/飞书LLM
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

本项目当前依赖来自 `requirements.txt`，主要包括：

- `fastapi`
- `uvicorn`
- `httpx`
- `python-dotenv`
- `loguru`
- `lark-oapi`
- `tenacity`

---

## 10. 启动项目

### 10.1 开发模式启动

在 Ubuntu 上启动命令建议使用：

```bash
cd ~/projects/飞书LLM
source .venv/bin/activate
python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

说明：

- `--reload` 适合开发调试
- `--host 0.0.0.0` 适合局域网或远程访问

如果你只想本机本地访问，也可以用：

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

### 10.2 首次启动建议观察

重点观察是否出现以下问题：

- `.env` 未读取
- OAuth 配置缺失
- DeepSeek API Key 缺失
- 端口占用
- 飞书回调访问失败

---

## 11. 迁移后必须检查的配置

### 11.1 `.env` 配置

重点检查以下字段：

- `APP_ID`
- `APP_SECRET`
- `DEEPSEEK_API_KEY`
- `DEEPSEEK_BASE_URL`
- `LLM_MODEL`
- `FEISHU_BASE_URL`
- `FEISHU_WEB_BASE_URL`
- `OAUTH_REDIRECT_URI`
- `INCLUDE_P2P_MESSAGE_SEARCH`
- `PROCESSING_TIMEOUT_SECONDS`

### 11.2 特别注意 `OAUTH_REDIRECT_URI`

这是迁移后最容易出问题的配置。

如果原来是本机开发地址：

```env
OAUTH_REDIRECT_URI=http://127.0.0.1:8000/oauth/callback
```

则要根据 Ubuntu 上的使用方式判断是否需要修改：

- 仅本机调试：可以继续保留 `127.0.0.1`
- 局域网访问：建议改成 Ubuntu 的局域网 IP
- 外网访问：建议改成公网域名或公网 IP

例如：

```env
OAUTH_REDIRECT_URI=http://192.168.1.100:8000/oauth/callback
```

或：

```env
OAUTH_REDIRECT_URI=https://your-domain.com/oauth/callback
```

---

## 12. 迁移后必须检查的飞书后台配置

如果 Ubuntu 机器的访问地址发生变化，必须同步检查飞书应用后台配置。

重点检查：

- OAuth 回调地址
- 事件订阅配置
- 应用凭证是否仍与 `.env` 一致
- 若使用外部访问，域名或 IP 是否已加入允许范围

如果这一步不检查，常见后果包括：

- 用户授权失败
- 回调失败
- 机器人无法正常收消息

---

## 13. 是否需要迁移 `.tokens` 和 `.memory`

### 13.1 建议迁移 `.tokens`

适合场景：

- 你不想让已有用户重新授权
- 你希望 Ubuntu 上直接沿用当前授权状态

### 13.2 建议迁移 `.memory`

适合场景：

- 你希望保留历史会话
- 你希望保留多轮上下文和话题

### 13.3 可以不迁移的情况

如果你只是把 Ubuntu 当一个全新开发环境，也可以不带：

- `.tokens/`
- `.memory/`

后果：

- 用户需要重新授权
- 历史会话丢失
- 但项目仍然可以正常重新跑起来

---

## 14. 迁移后验证清单

建议按下面顺序验证：

### 14.1 基础验证

- [ ] `python3 --version` 正常
- [ ] 虚拟环境已创建
- [ ] `pip install -r requirements.txt` 成功
- [ ] `uvicorn` 能正常启动

### 14.2 配置验证

- [ ] `.env` 已成功读取
- [ ] `APP_ID`、`APP_SECRET` 已配置
- [ ] `DEEPSEEK_API_KEY` 已配置
- [ ] `OAUTH_REDIRECT_URI` 正确

### 14.3 业务验证

- [ ] 能打开服务地址
- [ ] 飞书授权流程正常
- [ ] 机器人能收到消息
- [ ] 找人链路可以跑通
- [ ] 知识检索链路可以跑通
- [ ] 总结链路可以跑通

### 14.4 状态验证

- [ ] `.tokens/user_tokens.json` 可读
- [ ] `.memory/conversations.db` 可读
- [ ] 日志目录正常写入

---

## 15. 常见问题

### 15.1 项目启动了，但飞书授权失败

优先检查：

- `.env` 是否带过去了
- `OAUTH_REDIRECT_URI` 是否仍然指向旧机器
- 飞书后台回调地址是否已同步更新

### 15.2 项目启动了，但用户需要重新授权

优先检查：

- `.tokens/` 是否迁移成功
- `.tokens/user_tokens.json` 是否存在
- 文件权限是否正常

### 15.3 项目启动了，但历史上下文没了

优先检查：

- `.memory/` 是否迁移成功
- `.memory/conversations.db` 是否存在

### 15.4 `pip install` 失败

建议先升级 pip：

```bash
python -m pip install --upgrade pip
```

然后重试：

```bash
pip install -r requirements.txt
```

### 15.5 Ubuntu 上命令和 Windows 不一样

注意以下替换关系：

- Windows 激活虚拟环境：
  - `.\.venv\Scripts\Activate.ps1`
- Ubuntu 激活虚拟环境：
  - `source .venv/bin/activate`

---

## 16. 推荐的最短迁移流程

如果你想按最省事方式迁移，可以直接按下面执行：

### Windows 侧

1. 删除 `.venv`、`.venv_local`、`__pycache__`
2. 确认 `.env`、`.tokens`、`.memory` 是否保留
3. 将 `飞书LLM` 压缩为 zip 包
4. 把 zip 包下载或复制到 Ubuntu

### Ubuntu 侧

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git

mkdir -p ~/projects
cd ~/projects
unzip ~/Downloads/飞书LLM-ubuntu-migrate.zip
cd 飞书LLM

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt

python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

---

## 17. 迁移完成标准

满足以下条件即可认为迁移完成：

- Ubuntu 上项目目录完整
- Python 虚拟环境已重建
- 依赖已安装成功
- `.env` 正常生效
- 飞书 OAuth 正常
- 机器人能正常收发消息
- 找人、知识检索、总结链路可用

---

## 18. 额外建议

为了后续换电脑、备份和部署更轻松，建议你后面再补两件事：

- 增加 `.env.example`，把必需环境变量列清楚
- 增加一个 Ubuntu 启动脚本，例如 `start.sh`

这样后续换环境时会更省事。
