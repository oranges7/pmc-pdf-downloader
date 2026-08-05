"""PMC PDF 下载器 MCP Server (stdio 传输)。

通过 MCP 协议暴露 4 个工具供 LLM 客户端调用：
  - lookup_pmcid: 查询 PMID 对应的 PMCID
  - download_single_pdf: 下载单个 PMC PDF
  - batch_download_pdfs: 批量下载（含 CSV/TXT 输入、并行、重试）
  - get_download_report: 读取下载报告

环境变量：
  - PMC_PDF_EXECUTABLE_PATH: 可选，指定系统 Chrome/Edge 可执行文件路径
  - PLAYWRIGHT_BROWSERS_PATH: 可选，自定义 chromium 安装/查找目录
"""
import sys
import os
import atexit
from mcp.server.fastmcp import FastMCP

import pdf_core


mcp = FastMCP("pmc-pdf-downloader")


@mcp.tool()
def lookup_pmcid(pmid: str) -> str:
    """通过 NCBI elink API 查询 PMID 对应的 PMCID。

    Args:
        pmid: PubMed ID（如 "42534757"）
    Returns:
        PMCID（如 "PMC7440785"）；若文章未在 PMC 开放获取则返回空字符串
    """
    result = pdf_core.lookup_pmcid(pmid)
    return result or ""


@mcp.tool()
def download_single_pdf(pmcid: str, output_dir: str, pmid: str = "") -> str:
    """下载单个 PMC 文章的 PDF。已存在文件会自动跳过。

    Args:
        pmcid: PMCID（如 "PMC7440785"）
        output_dir: PDF 保存目录的绝对路径
        pmid: 可选 PMID，用于文件名前缀（默认空）
    Returns:
        成功返回 "OK: <bytes> bytes"，失败返回 "FAIL: <error>"
    """
    success, size, error = pdf_core.download_single_pdf(
        pmcid, output_dir, pmid or None
    )
    if success:
        return f"OK: {size} bytes -> {output_dir}"
    return f"FAIL: {error}"


@mcp.tool()
def batch_download_pdfs(
    input_file: str,
    output_dir: str,
    mode: str = "csv",
    num_workers: int = 2
) -> str:
    """批量下载 PMC PDF。自动跳过已存在的文件，失败自动重试 1 次。

    Args:
        input_file: 输入文件路径。CSV 需含 PMID+PMCID 两列；TXT 每行一个 PMID（会自动查询 PMCID）
        output_dir: PDF 保存目录的绝对路径
        mode: "csv" 或 "txt"，默认 "csv"
        num_workers: 并行下载线程数，默认 2
    Returns:
        汇总信息字符串，如 "完成: 成功 2400, 失败 36, 报告: <path>"
    """
    r = pdf_core.batch_download(input_file, output_dir, mode, num_workers)
    return (
        f"完成: 成功 {r['success']}, 失败 {r['failed']}, "
        f"报告: {r['report_file']}"
    )


@mcp.tool()
def get_download_report(output_dir: str) -> str:
    """读取指定目录下的 download_report.txt 下载报告内容。

    Args:
        output_dir: 之前批量下载时指定的输出目录
    Returns:
        报告文件全文。若文件不存在返回 "报告不存在"
    """
    content = pdf_core.get_download_report(output_dir)
    return content or "报告不存在"


def _check_chromium():
    """启动前检查 chromium 是否就绪，未装则给出警告（不退出，让 server 仍能注册工具）。"""
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            b.close()
        sys.stderr.write("[pmc-pdf-downloader] Chromium 检查通过\n")
    except Exception as e:
        sys.stderr.write(
            "[pmc-pdf-downloader] 警告: 未检测到 Chromium，下载工具将不可用。\n"
            "请运行：uv run playwright install chromium\n"
            "（如想用系统 Chrome，可设置环境变量 "
            "PMC_PDF_EXECUTABLE_PATH 指向 chrome.exe）\n"
            f"错误详情：{e}\n"
        )


def main():
    """MCP server 启动入口。"""
    _check_chromium()
    atexit.register(pdf_core.close_browser)
    mcp.run()


if __name__ == "__main__":
    main()
