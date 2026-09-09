# Notion Markdown API 同步指南

用自己的脚本替代 notionfs，通过 Notion Markdown API 把 Notion 内容拉到本地 markdown。
支持递归子页面、database、图片下载、并发拉取、自适应限速、Git 备份。


## 文件清单

- notion_sync.py — 主脚本，负责所有同步逻辑
- config.json — 你的 workspace 配置（token、要同步的页面）
- sync.sh — 一键同步 + git push 的 wrapper


## 0. 前置条件

- Python 3.10+（确认：python3 --version）
- requests 库（pip install requests）
- Git
- GitHub 账号，本地已配置 SSH 或 HTTPS 认证
- 两个 Notion workspace 的 PAT（创建方式见下方）


## 1. 创建 Personal Access Token

对每个 workspace：

1. 打开 https://www.notion.so/developers
2. 进入「Personal access tokens」
3. 点击「New token」
4. 名称随意（比如 sync-personal）
5. 选择对应的 workspace
6. Capabilities 勾选「Notion API」
7. 创建，复制 token（ntn_ 开头），妥善保存

PAT 继承你的用户权限，不需要逐页连接 integration。


## 2. 创建项目目录

```bash
mkdir -p ~/notion-sync
cd ~/notion-sync
```

把以下三个文件放进这个目录：
- notion_sync.py
- config.example.json（复制为 config.json 后编辑）
- sync.sh

```bash
cp config.example.json config.json
chmod +x sync.sh
chmod +x notion_sync.py
```


## 3. 编辑 config.json

```json
{
  "workspaces": [
    {
      "name": "personal",
      "token": "ntn_你的_personal_token",
      "output_dir": "./personal",
      "roots": [
        "https://www.notion.so/Projects-3ce935f391828099aaf5c544835cfa0b",
        "https://www.notion.so/Notes-abc123...",
        "https://www.notion.so/Reading-List-def456..."
      ]
    },
    {
      "name": "work",
      "token": "ntn_你的_work_token",
      "output_dir": "./work",
      "roots": [
        "https://www.notion.so/Work-Projects-ghi789..."
      ]
    }
  ]
}
```

roots 里填你想同步的所有顶层页面和 database 的 URL（或 page ID）。
脚本会自动检测每个 root 是 page 还是 database，并递归拉取所有子内容。

如果不填 roots（留空数组 `[]` 或者去掉这个字段），脚本会通过 Search API 自动发现 workspace 里所有顶层页面。

如何找到 page 或 database 的 URL：在 Notion 里打开页面，浏览器地址栏里的就是。


## 4. 首次同步

```bash
cd ~/notion-sync
pip install requests
python3 notion_sync.py
```

你会看到类似这样的输出：

```
============================================================
Syncing workspace: personal
Output: ./personal
Mode: incremental
============================================================

Root pages (3):
  • 1-Projects  (3ce935f3-...)
  • Notes  (abc123...)
  • Reading List  (def456...)

[page] 1-Projects
  [page] Project Alpha
  [page] Project Beta
[page] Notes
  [page] Meeting Notes
    [page] 2026-09-01
    [page] 2026-09-05
[database] Reading List
  12 rows
  [row] Designing Data-Intensive Applications
  [row] The Art of Statistics
  ...

[fetch] downloading content for 18 pages...

Done: 18 updated, 0 unchanged, 1 databases, 5 images, 0 deleted, 0 errors
```

脚本分三个阶段工作：
1. **Prefetch（预取）**：通过 Search API 批量获取所有页面的 `last_edited_time`（约 16 次请求），建立内存缓存。增量同步时大多数页面可以直接通过缓存判断是否有变化，无需逐页调用 API。`--full` 模式跳过此阶段。
2. **Pass 1（发现）**：遍历整棵树，找到所有页面和数据库行。对每个页面，先查 prefetch 缓存 → 再看父页面的 hint → 最后才调用 get_page（仅用于 Search 尚未索引的新页面）。把需要拉内容的加入队列。
3. **Pass 2（拉取）**：用 3 个线程并发拉取 Markdown 内容、下载图片、写文件

之后再次运行时，prefetch 缓存判断大多数页面无变化，直接跳过：

```
[prefetch] fetched edit times for 1596 pages (16 requests)

[skip] 1-Projects
  [skip] Project Alpha
  [page] Project Beta           ← 这个改过了，加入队列
[skip] Notes
  ...

[fetch] downloading content for 1 pages...

Done: 1 updated, 16 unchanged, 1 databases, 0 images, 0 deleted, 0 errors
```

同步完成后，你的目录结构大概是：

```
~/notion-sync/
  notion_sync.py
  config.json
  sync.sh
  personal/
    _images/
      a1b2c3d4e5f6g7h8.png
      ...
    1-Projects/
      _index.md
      Project Alpha.md
      Project Beta.md
    Notes/
      _index.md
      Meeting Notes/
        _index.md
        2026-09-01.md
        2026-09-05.md
    Reading List/
      Designing Data-Intensive Applications.md
      The Art of Statistics.md
  work/
    ...
```


## 5. 在 GitHub 上创建 Repo

创建两个 private repo（比如 notion-backup-personal 和 notion-backup-work），
不要勾选初始化 README。

然后分别初始化 git：

```bash
cd ~/notion-sync/personal
git init
cat > .gitignore << 'EOF'
.DS_Store
.manifest.json
EOF
git add .
git commit -m "Initial sync from Notion"
git remote add origin git@github.com:你的用户名/notion-backup-personal.git
git branch -M main
git push -u origin main
```

对 work 目录重复同样的操作。


## 6. 日常使用

每次想同步，跑一下：

```bash
cd ~/notion-sync
./sync.sh
```

sync.sh 会：
1. 跑 notion_sync.py 增量拉取变化内容（并发 + 自适应限速）
2. 对每个 workspace 目录做 git add、commit、push

刚在 Notion 里改完东西，想立刻同步？加 `--wait` 等待 Search API 索引完成：

```bash
python3 notion_sync.py --wait 45
```

只同步某个 workspace：

```bash
python3 notion_sync.py --workspace personal
```

先看看会拉什么，不实际写文件：

```bash
python3 notion_sync.py --dry-run
```

强制全量重新拉取（忽略 manifest，比如改了脚本逻辑后想刷新所有文件）：

```bash
python3 notion_sync.py --full
```


## 7.（可选）定时自动同步

```bash
crontab -e
```

添加：

```
0 * * * * cd ~/notion-sync && ./sync.sh >> ~/notion-sync/sync.log 2>&1
```


## 8. 配合 Claude Code 或 Claude Desktop 使用

Claude Code：

```bash
# 同步后直接让 Claude Code 读文件
cat ~/notion-sync/personal/1-Projects/Project\ Alpha.md

# 或者把 notion-sync 目录加到 Claude Code 的工作区
```

Claude Desktop（claude.ai）：上传需要的 markdown 文件即可。


## 9. 安全注意事项

config.json 里包含你的 PAT，不要提交到任何 Git repo。
在 ~/notion-sync/ 目录下加一个 .gitignore：

```bash
cat > ~/notion-sync/.gitignore << 'EOF'
config.json
sync.log
EOF
```

如果你想更安全，可以用环境变量替代 config.json 里的 token 值。
在 config.json 里写 "$NOTION_TOKEN_PERSONAL"，然后在 shell 里 export。
（需要小改 notion_sync.py 的 load_config 函数来做环境变量替换。）


## 性能

脚本使用 Search API 预取 + 三阶段架构 + 3 线程并发 + 自适应限速器（默认 4 req/s，遇到 429 自动降速，成功后恢复）。增量同步时，Search API 批量获取所有页面的编辑时间（约 16 次请求），绝大多数页面无需单独调用 API 即可判断是否有变化。

粗略估算（~1600 页的 workspace）：
- 首次全量同步：~4500 次 API 请求，约 20-25 分钟
- 增量同步（无变化）：~140 次请求（Search 预取 + 数据库查询），约 1 分钟
- 增量同步（少量变化）：~150 次请求，约 1 分钟

Manifest 每 20 秒自动保存一次，Ctrl-C 中断时也会保存，下次从中断处继续。


## Markdown API 的已知限制

以下 block 类型不会出现在 markdown 输出中，会显示为 <unknown> 标签：
- Bookmark（网页书签）
- Embed（嵌入的第三方内容）
- Link preview（链接预览卡片）
- Breadcrumb（面包屑导航）
- Template button（模板按钮，已弃用）

超过约 20,000 个 block 的超大页面会被截断，脚本会自动尝试补拉缺失的 block。

图片 URL 是临时签名的，本脚本会自动下载到 _images/ 目录并替换为本地路径，
所以备份是持久的。


## 常用命令速查

```bash
# 增量同步所有 workspace（默认行为）
cd ~/notion-sync && python3 notion_sync.py

# 刚改完 Notion，等待 Search API 索引后再同步
python3 notion_sync.py --wait 45

# 强制全量同步（忽略 manifest，跳过 prefetch）
python3 notion_sync.py --full

# 只同步 personal workspace
python3 notion_sync.py --workspace personal

# 预览模式（不写文件）
python3 notion_sync.py --dry-run

# 同步 + git push
./sync.sh

# 查看 git 变更历史
cd personal && git log --oneline

# 查看某个文件的历史版本
cd personal && git log -p "1-Projects/Project Alpha.md"
```
