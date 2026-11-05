"""Local invoice totals. No documents are uploaded to external services."""
from __future__ import annotations

import argparse
import base64
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import io
from pathlib import Path
import re
import sys
import unicodedata

import pymupdf


MONEY = re.compile(r"(?<![\d.])[-+]?\d+(?:,\d{3})*\.\d{2}(?![\d.])")


class RecognitionError(ValueError):
    pass


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).replace("−", "-")


@dataclass(frozen=True)
class Invoice:
    amount: Decimal
    number: str | None
    page: int


def page_amount(page: pymupdf.Page) -> Decimal | None:
    # Keep coordinates: PDF extraction order often places the total label far
    # away from its value in the extracted text (especially airline receipts).
    candidates = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = compact("".join(s["text"] for s in line["spans"]))
            for match in MONEY.finditer(text):
                candidates.append((pymupdf.Rect(line["bbox"]),
                                   Decimal(match.group().replace(",", ""))))
    text = compact(page.get_text())
    is_air = "航空" in text and "行程单" in text
    is_rail = "铁路电子客票" in text
    # An ordinary VAT invoice's bare 合计 is before tax, so never use it
    # as a substitute for 价税合计.
    labels = ("小写", "价税合计", "总计", "合计") if is_air else (
        "小写", "价税合计", "总计", "实付金额", "支付金额")
    # Railway e-tickets label the payable amount 票价 rather than 合计.
    # Restrict this fallback to railway tickets: on airline receipts and VAT
    # invoices a fare can be just one component of the final amount.
    if is_rail:
        labels += ("票价",)
    for label in labels:
        values = set()
        for anchor in page.search_for(label):
            ranked = []
            for rect, amount in candidates:
                mid = (rect.y0 + rect.y1) / 2
                amid = (anchor.y0 + anchor.y1) / 2
                height = max(anchor.height, rect.height, 6)
                # On the same row, the closest amount to the right.
                if abs(mid - amid) <= height * .65 and rect.x0 >= anchor.x0:
                    ranked.append((0, max(0, rect.x0 - anchor.x1), amount))
                # Airline totals are in a column below the label.
                elif is_air and 0 <= rect.y0 - anchor.y1 <= height * 3:
                    # OFD / airline templates left-align the column heading
                    # but right-align the currency amount within that column.
                    if anchor.x0 <= rect.x0 <= anchor.x1 + page.rect.width * .12:
                        ranked.append((1, (rect.y0 - anchor.y1) * 1000
                                       + abs(rect.x0 - anchor.x0), amount))
            if ranked:
                ranked.sort(key=lambda r: r[:2])
                best = ranked[0]
                tied = {r[2] for r in ranked if r[:2] == best[:2]}
                if len(tied) != 1:
                    raise RecognitionError("金额位置有歧义，请人工核对")
                values.add(best[2])
        if len(values) > 1:
            raise RecognitionError(f"同页存在多个不同的{label}金额，请拆分或人工核对")
        if values:
            return values.pop()
    return None


def labelled_numbers(page: pymupdf.Page, label: str, lengths: tuple[int, ...]) -> set[str]:
    # Use character bounds rather than whole-line bounds: the label, account
    # number and invoice number may share a PDF text line or extraction block.
    candidates = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            chars = []
            for span in line["spans"]:
                for char in span["chars"]:
                    chars.extend((c, char["bbox"]) for c in compact(char["c"]))
            text = "".join(c for c, _ in chars)
            for match in re.finditer(r"(?<!\d)\d+(?!\d)", text):
                if len(match.group()) not in lengths:
                    continue
                rect = pymupdf.Rect(chars[match.start()][1])
                for _, bounds in chars[match.start() + 1:match.end()]:
                    rect |= pymupdf.Rect(bounds)
                candidates.append((rect, match.group()))
    numbers = set()
    for anchor in page.search_for(label):
        ranked = []
        for rect, number in candidates:
            height = max(anchor.height, rect.height, 6)
            dy = abs((rect.y0 + rect.y1 - anchor.y0 - anchor.y1) / 2)
            if dy <= height * .65 and rect.x0 >= anchor.x1 - height * .5:
                ranked.append((0, max(0, rect.x0 - anchor.x1), number))
            elif (0 <= rect.y0 - anchor.y1 <= height * 2
                  and abs(rect.x0 - anchor.x0) <= height * 2):
                ranked.append((1, rect.y0 - anchor.y1, number))
        if ranked:
            best = min(r[:2] for r in ranked)
            numbers.update(r[2] for r in ranked if r[:2] == best)
    return numbers


def page_number(page: pymupdf.Page) -> str | None:
    # Digit length alone cannot distinguish invoice IDs from bank accounts.
    numbers = labelled_numbers(page, "发票号码", (8, 20))
    if len(numbers) > 1:
        raise RecognitionError("同页存在多个已标注的发票号码，请拆分后统计")
    if not numbers:
        return None
    number = numbers.pop()
    if len(number) == 20:
        return number
    codes = labelled_numbers(page, "发票代码", (10, 12))
    if len(codes) > 1:
        raise RecognitionError("同页存在多个已标注的发票代码，请人工核对")
    return f"{codes.pop()}:{number}" if codes else None


def read_pdf(path: Path) -> list[Invoice]:
    invoices = []
    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise RecognitionError("PDF 已加密，需要先解除密码")
        if not len(doc):
            raise RecognitionError("PDF 没有页面")
        for index, page in enumerate(doc):
            amount = page_amount(page)
            if amount is not None:
                invoices.append(Invoice(amount, page_number(page), index + 1))
            else:
                raise RecognitionError(f"第 {index + 1} 页未能识别合计金额（扫描件请先 OCR；附件页请拆分）")
    if not invoices:
        raise RecognitionError("未能识别合计金额，请人工核对或先 OCR")
    return invoices


def convert_ofd(source: Path, destination: Path) -> None:
    # easyofd emits verbose diagnostics; keep them for error messages only.
    diagnostics = io.StringIO()
    try:
        with redirect_stdout(diagnostics), redirect_stderr(diagnostics):
            from loguru import logger
            logger.remove()
            from easyofd import OFD
            ofd = OFD()
            try:
                ofd.read(base64.b64encode(source.read_bytes()).decode("ascii"))
                data = ofd.to_pdf()
            finally:
                ofd.del_data()
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if not len(doc):
                raise RecognitionError("转换结果没有页面")
            # Mark the source hash; later runs can reuse only our own output.
            metadata = dict(doc.metadata)
            metadata["subject"] = "fapiao-ofd-sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
            doc.set_metadata(metadata)
            data = doc.tobytes(garbage=3, deflate=True)
        # Exclusive creation protects unrelated PDFs and concurrent runs.
        with destination.open("xb") as output:
            output.write(data)
    except Exception as exc:
        raise RecognitionError(f"OFD 转换失败：{exc}") from exc


def converted_path(source: Path) -> Path:
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    names = [source.with_suffix(".pdf"), source.with_name(source.stem + ".ofd.pdf")]
    names.append(source.with_name(source.stem + ".ofd-" + digest[:12] + ".pdf"))
    for path in names:
        if not path.exists():
            convert_ofd(source, path)
            print(f"[转换] {source.name} -> {path.name}")
            return path
        try:
            with pymupdf.open(path) as doc:
                if doc.metadata.get("subject") == "fapiao-ofd-sha256:" + digest:
                    print(f"[已有] {source.name} -> {path.name}")
                    return path
        except Exception:
            pass
    raise RecognitionError("转换目标文件均已存在且不属于本工具，未覆盖任何文件")


def run(directory: Path) -> int:
    print(f"统计目录：{directory}")
    files = sorted((p for p in directory.iterdir() if p.is_file()), key=lambda p: p.name.casefold())
    pdfs = {p for p in files if p.suffix.lower() == ".pdf"}
    errors = []
    for source in files:
        if source.suffix.lower() == ".ofd":
            try:
                pdfs.add(converted_path(source))
            except Exception as exc:
                errors.append((source.name, str(exc)))
    total = Decimal("0.00")
    count = duplicates = 0
    seen_hashes: set[str] = set()
    seen_numbers: dict[str, Decimal] = {}
    for path in sorted(pdfs, key=lambda p: p.name.casefold()):
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest in seen_hashes:
                duplicates += 1
                print(f"[重复] {path.name}（内容相同，跳过）")
                continue
            invoices = read_pdf(path)
            # Validate all entries before counting any part of a file.
            pending = dict(seen_numbers)
            for invoice in invoices:
                if invoice.number:
                    if invoice.number in pending and pending[invoice.number] != invoice.amount:
                        raise RecognitionError(f"发票 {invoice.number} 与另一份金额不一致")
                    pending[invoice.number] = invoice.amount
            for invoice in invoices:
                if invoice.number and invoice.number in seen_numbers:
                    duplicates += 1
                    print(f"[重复] {path.name} 第{invoice.page}页（发票号码相同，跳过）")
                    continue
                if invoice.number:
                    seen_numbers[invoice.number] = invoice.amount
                count += 1
                total += invoice.amount
                print(f"[计入] {path.name} 第{invoice.page}页  ￥{invoice.amount:,.2f}")
            seen_hashes.add(digest)
        except Exception as exc:
            errors.append((path.name, str(exc)))
    for name, reason in errors:
        print(f"[待核对] {name}：{reason}")
    print(f"\n计入 {count} 张，跳过重复 {duplicates} 份，待核对 {len(errors)} 份")
    label = "已识别金额小计（有文件待核对）" if errors else "全部发票总额"
    print(f"{label}：￥{total:,.2f}")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="将当前目录 OFD 转为 PDF，汇总发票/行程单金额；不递归子目录。")
    parser.add_argument("directory", nargs="?", type=Path, default=Path.cwd(), help="可选：指定统计目录")
    args = parser.parse_args()
    if not args.directory.is_dir():
        parser.error("指定路径不是目录")
    return run(args.directory.resolve())


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
