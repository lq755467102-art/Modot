"""Untrusted document decoding runs only after the parent installs resource limits."""

# 主要引用或加载入口包括 `parser_process.py`。

import json
import sys

TABLE_MAX_SHEETS = 32
TABLE_MAX_ROWS = 2000
TABLE_MAX_COLUMNS = 128
TEXT_BUDGET = 120000


def _check_office_archive(archive, label):
    entries = archive.infolist()
    if (
        len(entries) > 2000
        or sum(entry.file_size for entry in entries) > 32 * 1024 * 1024
        or any(entry.file_size > 8 * 1024 * 1024 for entry in entries)
    ):
        raise ValueError(label + " 解压体积或成员数超限")
    if "[Content_Types].xml" not in archive.namelist():
        raise ValueError("不是有效的 " + label + " 包")


def _read_table(data, file_format, source_hash):
    import csv
    from io import BytesIO, StringIO

    workbook = None
    if file_format == "xlsx":
        import os
        from zipfile import ZipFile

        with ZipFile(BytesIO(data)) as archive:
            _check_office_archive(archive, "XLSX")
        os.environ["OPENPYXL_LXML"] = "False"
        os.environ["OPENPYXL_DEFUSEDXML"] = "True"
        import openpyxl
        from openpyxl.xml import DEFUSEDXML

        if not DEFUSEDXML:
            raise ValueError("缺少 XLSX 安全 XML 解析器")
        workbook = openpyxl.load_workbook(BytesIO(data), read_only=True, keep_links=False)
        parser = "openpyxl-" + openpyxl.__version__
        notes = ["单元格按工作表行号定位。公式显示原式，未执行计算。"]
        for sheet in workbook.worksheets[:TABLE_MAX_SHEETS]:
            sheet.reset_dimensions()
        tables = ((sheet.title, sheet.iter_rows(values_only=True)) for sheet in workbook.worksheets)
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("gb18030")
        if "\x00" in text:
            raise ValueError("表格文本包含二进制内容")
        csv.field_size_limit(len(data))
        delimiter = "," if file_format == "csv" else "\t"
        tables = [(file_format.upper(), csv.reader(StringIO(text, newline=""), delimiter=delimiter, strict=True))]
        parser = "delimited-text-v1"
        notes = ["行号按表格记录计数，带引号的跨行单元格仍属于同一行。"]

    sheets, segments, warnings = [], [], []
    remaining = TEXT_BUDGET
    try:
        for sheet_index, (name, rows) in enumerate(tables, 1):
            if sheet_index > TABLE_MAX_SHEETS:
                warnings.append(f"仅保留前 {TABLE_MAX_SHEETS} 个工作表")
                break
            locator = f"sheet-{sheet_index}"
            kept, columns, partial = 0, 0, False
            for index, cells in enumerate(rows, 1):
                if len(segments) >= TABLE_MAX_ROWS:
                    warnings.append(f"{name} 的剩余行超出全文件 {TABLE_MAX_ROWS} 行解析上限")
                    partial = True
                    break
                columns = max(columns, len(cells))
                if len(cells) > TABLE_MAX_COLUMNS:
                    partial = True
                data_line = json.dumps(cells[:TABLE_MAX_COLUMNS], ensure_ascii=False,
                                       separators=(",", ":"), default=lambda value: getattr(value, "text", str(value)))
                if len(data_line) > remaining:
                    warnings.append(f"{name} 从第 {index} 行起超过表格文字预算")
                    remaining = 0
                    partial = True
                    break
                remaining -= len(data_line)
                segments.append(dict(locator=f"{locator}-row-{index}", sheet=locator, row=index, text=data_line))
                kept += 1
            if columns > TABLE_MAX_COLUMNS:
                warnings.append(f"{name} 每行仅保留前 {TABLE_MAX_COLUMNS} 列")
            sheets.append(dict(locator=locator, name=name, rowsRead=kept,
                               columnsRead=min(columns, TABLE_MAX_COLUMNS), partial=partial))
    finally:
        if workbook is not None:
            workbook.close()
    return dict(state="partial" if warnings else "ready", sourceHash=source_hash,
                parserVersion=parser, sheets=sheets, segments=segments, warnings=warnings,
                notes=notes, total=len(segments))


# `parser_process.py` 提供隔离目录中的输入副本。
# 先核对内容哈希，再按 PDF 或文档结构提取带位置的文本段。
def parse(request):
    from pathlib import Path
    import hashlib
    from io import BytesIO

    data = Path(request["path"]).read_bytes()
    if (
        len(data) > 2 * 1024 * 1024
        or hashlib.sha256(data).hexdigest() != request["hash"]
    ):
        raise ValueError("Source changed")
    if request["format"] in {"csv", "tsv", "xlsx"}:
        return _read_table(data, request["format"], request["hash"])
    segments = []
    warnings = []
    remaining = TEXT_BUDGET

    # 所有页或段共享剩余字符预算；先截断，再保留非空内容及原定位。
    def add(locator, text):
        nonlocal remaining
        if not remaining:
            return
        text = text[:remaining]
        remaining -= len(text)
        if text.strip():
            segments.append({"locator": locator, "text": text})

    if request["format"] == "pdf":
        import pypdf

        parser = "pypdf-" + pypdf.__version__
        reader = pypdf.PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ValueError("PDF 受密码保护，尚未解析")
        total = len(reader.pages)
        if total > 100:
            warnings.append("超过 100 页，仅提取前 100 页")
        for i, page in enumerate(reader.pages):
            if i >= 100 or not remaining:
                break
            text = page.extract_text() or ""
            if not text.strip():
                warnings.append(f"第 {i + 1} 页无可提取文字，可能需要 OCR")
            resources = page.get("/Resources", {})
            if resources and resources.get("/XObject"):
                warnings.append(f"第 {i + 1} 页含图形资源；图片内容未识别")
            add("page-" + str(i + 1), text)
        notes = ["仅提取文本层，未还原表格布局或执行 OCR。"]
    else:
        from zipfile import ZipFile
        from xml.etree import ElementTree as ET

        parser = "docx-xml-v1-python-" + sys.version.split()[0]
        with ZipFile(BytesIO(data)) as archive:
            _check_office_archive(archive, "DOCX")
            xml = archive.read("word/document.xml")
            if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                raise ValueError("不支持含 XML 实体声明的文档")
            root = ET.fromstring(xml)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            body = root.find("w:body", ns)
            if body is None:
                raise ValueError("DOCX 缺少正文")
            paragraphs = body.findall("w:p", ns)
            total = len(paragraphs)
            for i, paragraph in enumerate(paragraphs[:1000]):
                if not remaining:
                    break
                add(
                    "paragraph-" + str(i + 1),
                    "".join(
                        node.text or ""
                        if node.tag == "{" + ns["w"] + "}t"
                        else "\t"
                        if node.tag == "{" + ns["w"] + "}tab"
                        else "\n"
                        if node.tag in {"{" + ns["w"] + "}br", "{" + ns["w"] + "}cr"}
                        else ""
                        for node in paragraph.iter()
                    ),
                )
            if any(
                child.tag
                not in {
                    "{" + ns["w"] + "}p",
                    "{" + ns["w"] + "}tbl",
                    "{" + ns["w"] + "}sectPr",
                }
                for child in body
            ):
                warnings.append("正文内容控件、自定义 XML 或其他包装内容未提取")
            if any(
                n in archive.namelist()
                for n in ["word/footnotes.xml", "word/endnotes.xml"]
            ):
                warnings.append("脚注或尾注内容未提取")
            if (
                body.find(".//w:drawing", ns) is not None
                or body.find(".//w:pict", ns) is not None
                or body.find(".//w:object", ns) is not None
            ):
                warnings.append("嵌入或链接的图形/对象未识别")
            if total > 1000:
                warnings.append("超过 1000 个正文段落，其余未提取")
            if body.find(".//w:tbl", ns) is not None:
                warnings.append("表格内容及结构未提取")
            if any(
                n.startswith("word/media/")
                or n.startswith("word/header")
                or n.startswith("word/footer")
                for n in archive.namelist()
            ):
                warnings.append("图片、页眉或页脚内容未提取")
            if (
                body.find(".//w:del", ns) is not None
                or body.find(".//w:ins", ns) is not None
            ):
                warnings.append("含修订记录，请核对原文")
        notes = ["定位使用正文段落序号，不推断 Word 渲染后的页码。"]
    if not remaining:
        warnings.append("文字达到 120000 字上限，其余未提取")
    return {
        "state": "partial" if warnings else "ready",
        "sourceHash": request["hash"],
        "parserVersion": parser,
        "segments": segments,
        "warnings": warnings,
        "notes": notes,
        "total": total,
    }


def main():
    # The parent sends this only after assigning the process to a memory-limited job.
    from pathlib import Path
    line = Path(sys.argv[1]).read_text(encoding='utf8') if len(sys.argv)==2 else sys.stdin.readline(16384)
    try:
        request = json.loads(line)
        if sys.platform != "win32":
            import resource

            resource.setrlimit(
                resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024)
            )
            resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
        result = parse(request)
    except Exception as error:
        result = {
            "state": "failed",
            "error": (type(error).__name__ + ": " + str(error))[:500],
        }
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
