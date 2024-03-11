"""Entry point for the scraper."""
import argparse
import json
import logging
import threading
import time
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from scraper.enums import lang_type, video_type
from scraper.exceptions import ScrapeError, StopSignal
from scraper.fake import fake_result
from scraper.functions import findfunc

_logger = logging.getLogger(__name__)

# define default scraping configuration path
_basedir = Path(__file__).resolve().parent
_flow_path = _basedir / "../scrapeflows"
_flowconf_path = _basedir / "../scrapeflows.conf"

# define maximum number of results to return
_maxlimit = 10
_results: List[Any] = []

def javformat(title) -> str:
    # 首先，移除所有非英数字符和非连字符，除了空格，以便后续将空格替换为连字符
    filtered_title = re.sub(r'[^\w\s-]', '', title)

    # 将连续的空格替换为单个空格，以便统一处理
    filtered_title = re.sub(r'\s+', ' ', filtered_title)
    
    # 将空格替换为连字符
    filtered_title = filtered_title.replace(' ', '-')
    
    # 使用正则表达式匹配符合 "英数-数字" 格式的字符串
    match = re.search(r'[A-Za-z0-9]+-[0-9]+', filtered_title)
    if match:
        return match.group()  # 返回匹配到的符合格式的部分
    else:
        # 如果没有找到符合格式的部分，返回调整后的字符串或原字符串
        # 根据实际需求可能返回 None 或其他值
        return filtered_title  # 或者 return None

def scrape(plugin_id: str) -> str:
    """Scrape video information from given arguments."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=str, required=True)
    parser.add_argument("--type", type=video_type, required=True)
    parser.add_argument("--lang", type=lang_type, required=False)
    parser.add_argument("--limit", type=int, default=_maxlimit)
    parser.add_argument("--allowguess", action="store_true", default=False)
    parser.add_argument("--loglevel", type=str, default="critical")

    args = parser.parse_known_args()[0]
    videotype = args.type.value
    language = args.lang.value if args.lang is not None else None
    maxlimit = min(args.limit, _maxlimit)
    loglevel = args.loglevel.upper()

    # set basic logging configuration
    logformat = (
        "%(asctime)s %(threadName)s %(levelname)s "
        "%(filename)s:%(lineno)d - %(message)s"
    )
    logging.basicConfig(level=getattr(logging, loglevel), format=logformat)

    # parse --input argument as JSON
    jsoninput = json.loads(args.input)
    if jsoninput["title"] == "--install":
        return fake_result(plugin_id, videotype)
    initialval = {
        "title": jsoninput["title"],
        "season": jsoninput.get("season", 0),
        "episode": jsoninput.get("episode", 1),
        "available": jsoninput.get("original_available", None),
        "year": str(jsoninput.get("original_available", ""))[:4],
        "lang": language,
        "limit": maxlimit,
        "version": _version(plugin_id),
    }

    # load and execute scrape flows using multithreading
    start = time.time()
    taskqueue: Dict[int, List[threading.Thread]] = {}
    for flow in ScrapeFlow.load(_flow_path, videotype, language, initialval):
        # format jav number
        if flow.site == "av-wiki.net":
            flow.context["title"]= javformat(initialval["title"])

        task = threading.Thread(target=_start, args=(flow, maxlimit))
        tasks = taskqueue.get(flow.priority, [])
        tasks.append(task)
        taskqueue[flow.priority] = tasks
    for tasks in dict(sorted(taskqueue.items(), key=lambda x: x[0])).values():
        if len(_results) >= maxlimit:
            break
        for task in tasks:
            task.start()
        for task in tasks:
            task.join()
    end = time.time()
    _logger.info("Total execution time: %.3f seconds", end - start)
    return json.dumps(
        {"success": True, "result": _results}, ensure_ascii=False, indent=2
    ).replace("[plugin_id]", plugin_id)


def _start(flow: "ScrapeFlow", limit: int):
    """Start a scrape flow and store results."""
    try:
        result_gen = flow.start()
        while True:
            if len(_results) >= limit:
                break
            try:
                _results.append(next(result_gen))
            except StopIteration:
                break
    except ScrapeError:
        _logger.error("Failed to scrape from %s", flow.site, exc_info=True)


def _version(plugin_id: str) -> str:
    """Split the plugin ID to get the version."""
    if "-" in plugin_id:
        version = plugin_id.split("-")[-1]
        if version != "plugin":
            return f"/{version}"
    return ""


class ScrapeFlow:
    """A flow of steps to scrape video information."""

    def __init__(
        self,
        site: str,
        steps: list,
        context: dict,
        priority: Optional[int],
    ):
        self.site = site
        self.steps = steps
        self.context = context
        self.priority = priority if priority is not None else 999

    def start(self):
        """Start the scrape flow and return a generator."""
        for funcname, rawargs in [s.popitem() for s in self.steps]:
            # execute the function with context
            try:
                iterable = findfunc(funcname)(rawargs, self.context)
                if iterable is not None:
                    yield from iterable
            except StopSignal:
                break

    @staticmethod
    def load(path: Path, videotype: str, language: str, initialval: dict):
        """Load scrape flows from given path."""

        flowconf = None
        if _flowconf_path.exists():
            with open(_flowconf_path, "r", encoding="utf-8") as conf_reader:
                flowconf = json.load(conf_reader)

        for filepath in path.glob("*.json"):
            with open(filepath, "r", encoding="utf-8") as def_reader:
                flowdef = json.load(def_reader)
            site = flowdef["site"]
            siteconf = None
            if flowconf is not None and site in flowconf:
                siteconf = flowconf[site]

            # filter out flows that do not match the video type
            if not ScrapeFlow.valid(flowdef, siteconf, videotype, language):
                continue

            # generate a flow instance from the definition
            steps = list(flowdef["steps"])
            context = initialval.copy()
            context["site"] = site
            context["doh"] = flowdef.get("doh_enabled", False)
            priority = None
            if siteconf is not None:
                priority = siteconf["priority"]
                context.update(siteconf)
            yield ScrapeFlow(site, steps, context, priority)

    @staticmethod
    def valid(flowdef: Any, siteconf: Any, videotype: str, language: str):
        """Check if the flow definition is valid."""

        if language is not None and "lang" in flowdef:
            if language not in flowdef["lang"]:
                return False

        if flowdef["type"] != videotype:
            return False

        if siteconf is not None:
            if not any(videotype.startswith(t) for t in siteconf["types"]):
                return False

        return True
