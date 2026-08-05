"""PMC PDF 下载核心模块 - 无 UI 依赖，供 GUI 和 MCP server 共享调用。

抽取自 pdf_downloader_ui.py 的核心下载逻辑，去掉 self._log / self.should_stop 等 UI 状态，
改为通过 log 回调和局部 should_stop 标志管理。
"""
import os
import csv
import json
import time
import threading
import urllib.request
from typing import Callable, Optional, Dict, Tuple
import queue as queue_module


ProgressCallback = Optional[Callable[[str], None]]


# ============ 浏览器实例池（进程级单例）============
_browser_lock = threading.Lock()
_browser_instance = None
_playwright_instance = None


def _get_browser():
    """惰性获取共享 Chromium 实例。首次调用启动，后续复用。
    支持 PMC_PDF_EXECUTABLE_PATH 环境变量指定系统 Chrome。
    """
    global _browser_instance, _playwright_instance
    from playwright.sync_api import sync_playwright
    with _browser_lock:
        if _browser_instance is None:
            executable = os.environ.get("PMC_PDF_EXECUTABLE_PATH")
            _playwright_instance = sync_playwright().start()
            if executable:
                _browser_instance = _playwright_instance.chromium.launch(
                    headless=True, executable_path=executable
                )
            else:
                _browser_instance = _playwright_instance.chromium.launch(headless=True)
        return _browser_instance


def close_browser():
    """显式关闭浏览器（MCP server 退出时调用）。"""
    global _browser_instance, _playwright_instance
    with _browser_lock:
        if _browser_instance is not None:
            try:
                _browser_instance.close()
            except Exception:
                pass
            _browser_instance = None
        if _playwright_instance is not None:
            try:
                _playwright_instance.stop()
            except Exception:
                pass
            _playwright_instance = None


# ============ 能力 1：PMCID 查询 ============
def lookup_pmcid(pmid: str, log: ProgressCallback = None) -> Optional[str]:
    """通过 NCBI elink API 查询 PMID 对应的 PMCID。

    Args:
        pmid: PubMed ID
        log: 可选日志回调
    Returns:
        PMCID 字符串（如 "PMC123456"）或 None
    """
    elink_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi"
        f"?dbfrom=pubmed&linkname=pubmed_pmc&id={pmid}&retmode=json"
    )
    try:
        with urllib.request.urlopen(elink_url, timeout=30) as response:
            data = json.loads(response.read().decode())
        for ls in data.get('linksets', []):
            for lsd in ls.get('linksetdbs', []):
                links = lsd.get('links', [])
                if links:
                    return f"PMC{links[0]}"
    except Exception as e:
        if log:
            log(f"PMCID 查询失败 {pmid}: {e}")
    return None


# ============ 能力 2：单篇 PDF 下载 ============
def download_single_pdf(
    pmcid: str,
    output_dir: str,
    pmid: Optional[str] = None,
    log: ProgressCallback = None
) -> Tuple[bool, int, Optional[str]]:
    """下载单个 PMC 文章的 PDF。

    Args:
        pmcid: PMCID（如 "PMC7440785"）
        output_dir: 输出目录
        pmid: 可选 PMID，用于文件名前缀
        log: 可选日志回调
    Returns:
        (success, size_bytes, error_message)
    """
    os.makedirs(output_dir, exist_ok=True)
    pmc_num = pmcid.replace('PMC', '')
    prefix = f"{pmid}_" if pmid else ""
    filepath = os.path.join(output_dir, f"{prefix}{pmcid}.pdf")

    if os.path.exists(filepath):
        if log:
            log(f"{pmcid} 已存在，跳过")
        return True, os.path.getsize(filepath), None

    pdf_url = f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmc_num}/pdf/main.pdf"
    browser = _get_browser()

    context = browser.new_context(
        user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        ),
        accept_downloads=True
    )
    try:
        page = context.new_page()
        page.goto(pdf_url, wait_until='commit', timeout=60000)

        # 等 PoW cookie（最多 30 秒）
        for _ in range(30):
            time.sleep(1)
            if any('pow' in c['name'].lower() for c in context.cookies()):
                break

        response = context.request.get(pdf_url, timeout=600000)

        if response.status == 200:
            content_type = response.headers.get('content-type', '')
            if 'pdf' in content_type.lower():
                pdf_bytes = response.body()
                with open(filepath, 'wb') as f:
                    f.write(pdf_bytes)
                if log:
                    log(f"{pmcid} 下载成功 {len(pdf_bytes)/1024/1024:.2f}MB")
                return True, len(pdf_bytes), None
            return False, 0, f"非PDF内容: {content_type}"
        return False, 0, f"HTTP {response.status}"
    except Exception as e:
        return False, 0, str(e)[:80]
    finally:
        context.close()


# ============ 能力 3：批量下载 ============
def batch_download(
    input_file: str,
    output_dir: str,
    mode: str = "csv",
    num_workers: int = 2,
    log: ProgressCallback = None
) -> Dict:
    """批量下载 PMC PDF。

    Args:
        input_file: CSV（含 PMID+PMCID）或 TXT（仅 PMID）路径
        output_dir: 输出目录
        mode: "csv" 或 "txt"
        num_workers: 并行线程数（默认 2）
        log: 可选日志回调
    Returns:
        {
            "total": int, "pmc_count": int, "no_pmc_count": int,
            "success": int, "failed": int,
            "failed_list": [{"pmid":..., "pmcid":..., "error":...}, ...],
            "report_file": str
        }
    """
    def _log(msg: str):
        if log:
            log(msg)

    pmc_map: Dict[str, Optional[str]] = {}

    # 1. 读取输入
    try:
        if mode == "csv":
            _log(f"从CSV文件读取PMCID: {input_file}")
            with open(input_file, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    pmid = row.get('PMID', '').strip() or row.get('\ufeffPMID', '').strip()
                    pmcid = row.get('PMCID', '').strip()
                    if pmid:
                        pmc_map[pmid] = pmcid if pmcid else None
            _log(f"从CSV读取了 {len(pmc_map)} 条记录")
        else:
            _log(f"从TXT文件读取PMID: {input_file}")
            with open(input_file, 'r') as f:
                pmids = [line.strip() for line in f if line.strip()]
            _log(f"共有 {len(pmids)} 个PMID需要查询")

            _log("正在查询PMC ID...")
            for i, pmid in enumerate(pmids, 1):
                pmcid = lookup_pmcid(pmid, log=log)
                pmc_map[pmid] = pmcid
                if pmcid:
                    _log(f"  [{i}/{len(pmids)}] {pmid} -> {pmcid}")
                else:
                    _log(f"  [{i}/{len(pmids)}] {pmid} -> 无PMC")
                time.sleep(0.35)
    except Exception as e:
        _log(f"读取输入文件失败: {e}")
        return {
            "total": 0, "pmc_count": 0, "no_pmc_count": 0,
            "success": 0, "failed": 0, "failed_list": [],
            "report_file": ""
        }

    pmids = list(pmc_map.keys())
    pmc_count = len([v for v in pmc_map.values() if v])
    no_pmc_count = len([v for v in pmc_map.values() if not v])
    _log(f"\n有PMC ID: {pmc_count} 个, 无PMC ID: {no_pmc_count} 个")

    # 2. 跳过已存在文件
    downloaded_pmids = set()
    for pmid in pmids:
        pmcid = pmc_map.get(pmid)
        if not pmcid:
            continue
        filename = f"{pmid}_{pmcid}.pdf"
        filepath = os.path.join(output_dir, filename)
        if os.path.exists(filepath):
            downloaded_pmids.add(pmid)
    _log(f"已存在文件: {len(downloaded_pmids)} 个")

    need_download = []
    for pmid in pmids:
        pmcid = pmc_map.get(pmid)
        if not pmcid:
            continue
        if pmid in downloaded_pmids:
            continue
        need_download.append((pmid, pmcid))

    _log(f"待下载: {len(need_download)} 个")

    if not need_download:
        _log("所有可下载的PDF已完成!")

    # 3. 并行下载（2 worker 线程，失败重试 1 次）
    failed_downloads = []
    if need_download:
        # 强制依赖 Playwright
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            _log("错误: 未检测到 Playwright，无法下载。请安装: pip install playwright && playwright install chromium")
            return {
                "total": len(pmids), "pmc_count": pmc_count,
                "no_pmc_count": no_pmc_count,
                "success": len(downloaded_pmids), "failed": 0,
                "failed_list": [], "report_file": ""
            }

        _log("\n开始下载PDF文件...")
        total_count = len(need_download)
        task_queue = queue_module.Queue()
        for task in need_download:
            task_queue.put(task)

        stats_lock = threading.Lock()
        completed_count = [0]

        def worker(worker_id: int):
            try:
                # 每个 worker 复用进程级单例 browser（不嵌套 sync_playwright）
                browser = _get_browser()
                _log(f"[Worker {worker_id}] 浏览器已就绪")

                while True:
                    try:
                        pmid, pmcid = task_queue.get_nowait()
                    except queue_module.Empty:
                        break

                    filename = f"{pmid}_{pmcid}.pdf"
                    filepath = os.path.join(output_dir, filename)

                    _log(f"[W{worker_id}] {pmid} ({pmcid}) 加载PoW...")

                    # 第一次尝试
                    success, size, error = _download_with_context(browser, pmcid, filepath)

                    # 失败重试 1 次
                    if not success:
                        _log(f"[W{worker_id}] {pmcid} 失败({error})，3秒后重试...")
                        time.sleep(3)
                        success, size, error = _download_with_context(browser, pmcid, filepath)

                    with stats_lock:
                        completed_count[0] += 1
                        idx = completed_count[0]

                        if success:
                            downloaded_pmids.add(pmid)
                            size_mb = size / 1024 / 1024
                            _log(f"[W{worker_id}] [{idx}/{total_count}] {pmcid} 成功! {size_mb:.2f}MB")
                        else:
                            failed_downloads.append({'pmid': pmid, 'pmcid': pmcid, 'error': error})
                            _log(f"[W{worker_id}] [{idx}/{total_count}] {pmcid} 失败: {error}")

                    task_queue.task_done()
                    time.sleep(1)

                _log(f"[Worker {worker_id}] 已退出")
            except Exception as e:
                _log(f"[Worker {worker_id}] 启动失败: {e}")
                _log(f"[Worker {worker_id}] 请确保已安装 Playwright 浏览器: playwright install chromium")

        _log(f"启动 {num_workers} 个并行下载线程...")
        threads = []
        for i in range(num_workers):
            t = threading.Thread(target=worker, args=(i + 1,), daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join()

    # 4. 生成报告
    report_file = os.path.join(output_dir, "download_report.txt")
    no_pmc_list = [pmid for pmid in pmids if not pmc_map.get(pmid)]

    try:
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write("=" * 60 + "\n")
            f.write("PDF下载报告\n")
            f.write("=" * 60 + "\n\n")
            f.write(f"总计PMID: {len(pmids)}\n")
            f.write(f"有PMC ID: {pmc_count}\n")
            f.write(f"无PMC ID: {no_pmc_count}\n")
            f.write(f"下载成功: {len(downloaded_pmids)}\n")
            f.write(f"下载失败: {len(failed_downloads)}\n\n")

            if no_pmc_list:
                f.write("=" * 60 + "\n")
                f.write(f"无PMC ID的PMID ({len(no_pmc_list)}个):\n")
                f.write("-" * 40 + "\n")
                for pmid in no_pmc_list:
                    f.write(f"  {pmid} - 文章未在PMC中开放获取\n")

            if failed_downloads:
                f.write("\n" + "=" * 60 + "\n")
                f.write(f"下载失败的PMID ({len(failed_downloads)}个):\n")
                f.write("-" * 40 + "\n")
                for item in failed_downloads:
                    f.write(f"  PMID {item['pmid']} ({item['pmcid']}): {item['error']}\n")

        _log(f"\n报告已保存到: {report_file}")
    except Exception as e:
        _log(f"保存报告失败: {e}")

    _log(f"\n{'='*40}")
    _log(f"下载完成! 成功: {len(downloaded_pmids)}, 失败: {len(failed_downloads)}")

    return {
        "total": len(pmids),
        "pmc_count": pmc_count,
        "no_pmc_count": no_pmc_count,
        "success": len(downloaded_pmids),
        "failed": len(failed_downloads),
        "failed_list": failed_downloads,
        "report_file": report_file,
    }


def _download_with_context(browser, pmcid: str, filepath: str) -> Tuple[bool, int, Optional[str]]:
    """使用进程级 browser 创建新 context 下载单个 PDF（worker 内部辅助函数）。"""
    pmc_num = pmcid.replace('PMC', '')
    pdf_url = f"https://pmc.ncbi.nlm.nih.gov/articles/PMC{pmc_num}/pdf/main.pdf"

    context = browser.new_context(
        user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        ),
        accept_downloads=True
    )
    try:
        page = context.new_page()
        page.goto(pdf_url, wait_until='commit', timeout=60000)

        for _ in range(30):
            time.sleep(1)
            if any('pow' in c['name'].lower() for c in context.cookies()):
                break

        response = context.request.get(pdf_url, timeout=600000)

        if response.status == 200:
            content_type = response.headers.get('content-type', '')
            if 'pdf' in content_type.lower():
                pdf_bytes = response.body()
                with open(filepath, 'wb') as f:
                    f.write(pdf_bytes)
                return True, len(pdf_bytes), None
            return False, 0, f"非PDF内容: {content_type}"
        return False, 0, f"HTTP {response.status}"
    except Exception as e:
        return False, 0, str(e)[:80]
    finally:
        context.close()


# ============ 能力 4：读取下载报告 ============
def get_download_report(output_dir: str) -> Optional[str]:
    """读取指定输出目录下的 download_report.txt 内容。

    Args:
        output_dir: 输出目录
    Returns:
        报告全文；文件不存在返回 None
    """
    report_file = os.path.join(output_dir, "download_report.txt")
    if not os.path.exists(report_file):
        return None
    with open(report_file, 'r', encoding='utf-8') as f:
        return f.read()
