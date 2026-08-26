"""Recording what a run would have written, without writing it."""

import json
import os
import time
import zipfile
from difflib import unified_diff


class Archive:
    """The dry run output: a report and a diff per document, in one zip."""

    def __init__(self, run_id, would_tag):
        self.run_id = run_id or "local"
        self.would_tag = would_tag
        self.zip = None
        self.path = None

    def add(self, document, pages, report):
        """Record what would have been written to this document.

        Two views of the same run: a JSON report holding the exact payload,
        and a unified diff per page, which is the quickest way to see that
        only field values were added.  Both go straight into the archive, so
        a batch of thousands leaves nothing else on disk.
        """
        if self.zip is None:
            timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
            self.path = f"{self.run_id}_{timestamp}_dry-run.zip"
            self.zip = zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED)

        entry = {
            "document_id": document.id,
            "would_tag": self.would_tag,
            "pages": [],
        }
        payload = {page["page_number"]: page for page in pages}
        for item in report:
            record = dict(item)
            page = payload.get(item["page_number"])
            if page is not None:
                record["positions"] = page["positions"]
            entry["pages"].append(record)

        self.zip.writestr(
            f"{document.id}.json",
            json.dumps(entry, ensure_ascii=False, indent=1),
        )

        diff = []
        for item in report:
            if item["action"] != "patch":
                continue
            diff.extend(
                unified_diff(
                    item["text_before"].splitlines(keepends=True),
                    item["text_after"].splitlines(keepends=True),
                    fromfile=f"page {item['page_number']} before",
                    tofile=f"page {item['page_number']} after",
                )
            )
            diff.append("\n")
        self.zip.writestr(f"{document.id}.diff", "".join(diff))

    def close(self):
        """Close the archive if one was opened, returning where it was written."""
        if self.zip is None or self.path is None:
            return None
        self.zip.close()
        path = self.path
        self.zip, self.path = None, None
        return path

    def discard(self):
        """Drop a half-written archive left behind by a run that died."""
        path = self.close()
        if path is not None and os.path.exists(path):
            os.remove(path)
