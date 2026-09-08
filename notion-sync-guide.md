# Notion Markdown API 同步指南

用自己的脚本替代 notionfs，通过 Notion Markdown API 把 Notion 内容拉到本地 markdown。
支持递归子页面、database、图片下载、双 workspace、Git 备份。


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
Roots: 3
============================================================

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

Done: 18 pages, 1 databases, 5 images downloaded, 0 errors
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
1. 跑 notion_sync.py 拉取两个 workspace 的最新内容
2. 对每个 workspace 目录做 git add、commit、push

只同步某个 workspace：

```bash
python3 notion_sync.py --workspace personal
```

先看看会拉什么，不实际写文件：

```bash
python3 notion_sync.py --dry-run
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
# 同步所有 workspace
cd ~/notion-sync && python3 notion_sync.py

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
