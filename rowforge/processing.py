import csv
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class InvalidCSV(ValueError):
    pass


class LostLease(Exception):
    pass


def safe_cell(value):
    # Downloads are spreadsheet-safe, including whitespace-prefixed formulas.
    return (
        "'" + value
        if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r"))
        else value
    )


def process(source, directory, rules, max_rows, batch_size, checkpoint):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    counts = dict(processed=0, accepted=0, rejected=0, duplicates=0, progress_bytes=0)
    seen = set()
    try:
        with (
            open(source, "r", encoding="utf-8-sig", newline="") as src,
            open(directory / "cleaned.csv", "w", encoding="utf-8", newline="") as out,
            open(directory / "errors.csv", "w", encoding="utf-8", newline="") as err,
        ):
            reader = csv.reader(src, strict=True)
            header = next(reader, None)
            if not header:
                raise InvalidCSV("The CSV is empty. Include a header row.")
            header = [h.strip() for h in header]
            if any(not h for h in header) or len(header) != len(set(header)):
                raise InvalidCSV("Column names must be nonempty and unique after trimming.")
            if len(header) > 100:
                raise InvalidCSV("The CSV exceeds the 100-column limit.")
            requested = set(rules["required"] + rules["email"] + rules["numeric"])
            if requested - set(header):
                raise InvalidCSV("Missing rule columns: " + ", ".join(sorted(requested - set(header))))
            writer, errors = csv.writer(out), csv.writer(err)
            writer.writerow([safe_cell(h) for h in header])
            errors.writerow(["source_line", "validation_errors"] + [safe_cell(h) for h in header])
            checkpoint(counts)
            for row in reader:
                if counts["processed"] >= max_rows:
                    raise InvalidCSV(f"The CSV exceeds the {max_rows:,}-row limit.")
                counts["processed"] += 1
                row = [v.strip() if rules["trim"] else v for v in row]
                problems = []
                if len(row) != len(header):
                    problems.append(f"Expected {len(header)} fields; found {len(row)}")
                values = dict(zip(header, row))
                for col in rules["required"]:
                    if not values.get(col, "").strip():
                        problems.append(f"{col}: required value missing")
                for col in rules["email"]:
                    value = values.get(col, "")
                    if value and not EMAIL.fullmatch(value):
                        problems.append(f"{col}: invalid email format")
                for col in rules["numeric"]:
                    value = values.get(col, "")
                    if value:
                        try:
                            if not Decimal(value).is_finite():
                                raise InvalidOperation
                        except InvalidOperation:
                            problems.append(f"{col}: expected a finite number")
                if problems:
                    counts["rejected"] += 1
                    errors.writerow([reader.line_num, "; ".join(problems)] + [safe_cell(v) for v in row])
                elif rules["dedupe"] and tuple(row) in seen:
                    counts["duplicates"] += 1
                else:
                    if rules["dedupe"]:
                        seen.add(tuple(row))
                    counts["accepted"] += 1
                    writer.writerow([safe_cell(v) for v in row])
                if counts["processed"] % batch_size == 0:
                    counts["progress_bytes"] = src.buffer.tell()
                    checkpoint(counts)
            counts["progress_bytes"] = Path(source).stat().st_size
            checkpoint(counts)
    except (UnicodeError, csv.Error) as exc:
        raise InvalidCSV("Use a valid UTF-8, comma-separated CSV: " + str(exc)) from exc
    return counts
