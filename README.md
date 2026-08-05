# PMC PDF 下载器 MCP Server

通过 MCP 协议批量下载 PMC 开放获取 PDF，支持 PoW 验证自动绕过、并行下载、失败重试。

## 快速开始

### 1. 安装 uv（一次性）

uv 是单一二进制的 Python 包管理工具，安装命令一行：

```bash
# Mac/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows PowerShell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. 安装 Chromium（一次性）

Playwright 的 chromium 浏览器无法通过 pip 安装，需手动运行一次：

```bash
# 方式 A：使用 uvx 直接从 git 仓库运行（推荐，无需克隆）
uvx --from git+https://github.com/oranges7/pmc-pdf-downloader playwright install chromium

# 方式 B：克隆后本地运行
git clone https://github.com/oranges7/pmc-pdf-downloader
cd pmc-pdf-downloader
uv sync
uv run playwright install chromium
```

### 3. 配置 MCP 客户端

#### Trae IDE / Claude Desktop

编辑 MCP 配置文件（如 `claude_desktop_config.json`），加入：

```json
{
  "mcpServers": {
    "pmc-pdf-downloader": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/oranges7/pmc-pdf-downloader",
        "pmc-pdf-downloader"
      ]
    }
  }
}
```

#### Cursor

Settings → MCP → Add New MCP Server：

- Type: `stdio`
- Command: `uvx`
- Args: `--from git+https://github.com/oranges7/pmc-pdf-downloader pmc-pdf-downloader`

#### 本地克隆版（不想推 GitHub 时用）

```json
{
  "mcpServers": {
    "pmc-pdf-downloader": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "D:/path/to/pmc-pdf-downloader",
        "pmc-pdf-downloader"
      ]
    }
  }
}
```

### 4. 使用

配置完成后，LLM 即可调用以下 4 个工具：

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `lookup_pmcid` | 查询 PMID 对应的 PMCID | `pmid` |
| `download_single_pdf` | 下载单个 PMC PDF | `pmcid`, `output_dir`, `pmid?` |
| `batch_download_pdfs` | 批量下载（CSV/TXT 输入） | `input_file`, `output_dir`, `mode?`, `num_workers?` |
| `get_download_report` | 读取下载报告全文 | `output_dir` |

**示例对话**：
- "帮我查 PMID 42534757 的 PMCID"
- "下载 PMC7440785 的 PDF 到 D:/papers"
- "批量下载 input.csv 里的所有 PDF 到 D:/papers，CSV 格式"
- "看一下 D:/papers 的下载报告"

## 关于浏览器

默认情况下**无需配置浏览器位置**。运行 `playwright install chromium` 后，浏览器会自动安装到用户目录的默认位置，MCP server 启动时自动查找：

- Windows: `%USERPROFILE%\AppData\Local\ms-playwright\chromium-XXXX`
- Mac/Linux: `~/.cache/ms-playwright/chromium-XXXX`

可选环境变量（特殊场景）：

| 环境变量 | 用途 | 示例 |
|---|---|---|
| `PLAYWRIGHT_BROWSERS_PATH` | 自定义 chromium 安装/查找目录（如系统盘空间不足时） | `D:/playwright-browsers` |
| `PMC_PDF_EXECUTABLE_PATH` | 直接指向系统已装的 Chrome/Edge 可执行文件，跳过 chromium 下载 | `C:\Program Files\Google\Chrome\Application\chrome.exe` |

## 本地开发

```bash
git clone https://github.com/oranges7/pmc-pdf-downloader
cd pmc-pdf-downloader
uv sync                              # 自动建虚拟环境、装依赖
uv run playwright install chromium   # 一次性
uv run pmc-pdf-downloader            # 启动 MCP server
```

## 输入文件格式

### CSV 模式

含两列：`PMID` 和 `PMCID`：

```csv
PMID,PMCID
42534757,PMC7440785
37984867,PMC10787064
```

### TXT 模式

每行一个 PMID，server 会自动调 NCBI elink API 查询 PMCID：

```text
42534757
37984867
38133632
```

## 输出

- PDF 文件命名：`<PMID>_<PMCID>.pdf`（如 `42534757_PMC7440785.pdf`）
- 已存在文件自动跳过
- 下载报告：`<output_dir>/download_report.txt`，含成功/失败统计与失败原因
