"""
Recovers texts inside PDF form fields.

This Add-On downloads the PDF, reads the values out of the form, and writes
them into the page text along with their estimated word positions.
"""

import itertools
import json
import os
import subprocess
import sys
import time
from collections import Counter

import requests
from documentcloud.addon import AddOn
from documentcloud.exceptions import APIError

from dry_run import Archive

TAG_KEY = "forms_text_recovery_addon"
TAG_VALUE = "v1"
FAILED_VALUE = "failed"

USER_AGENT = "Disclose Forms Text Recovery Add-On"

PAGE_CHUNK_SIZE = 20

# seconds
RETRY_EVERY = 5
MAX_WAIT_UPLOAD_PAGES = 300
MAX_WAIT_TAG_DOCUMENT = 60
ANALYZE_TIMEOUT = 300

# Run recover.analyze in a separate process, so a native PDFium
# abort on a malformed PDF can't take the whole run down.
WORKER = os.path.join(os.path.dirname(__file__), "subprocess_worker.py")


class AnalysisCrashed(Exception):
    """The analysis subprocess died (e.g. a PDFium native abort) or timed out."""


def analyze_isolated(pdf_bytes, keep_diff):
    """Run `recover.analyze` in a subprocess and return its `(pages, report)`."""
    cmd = [sys.executable, WORKER] + (["--keep-diff"] if keep_diff else [])
    try:
        result = subprocess.run(
            cmd,
            input=pdf_bytes,
            capture_output=True,
            timeout=ANALYZE_TIMEOUT,
            check=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise AnalysisCrashed(f"timed out after {ANALYZE_TIMEOUT}s") from exc
    except subprocess.CalledProcessError as exc:
        code = exc.returncode
        how = f"killed by signal {-code}" if code < 0 else f"exited with code {code}"
        tail = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        detail = f": {tail[-1]}" if tail else ""
        raise AnalysisCrashed(f"{how}{detail}") from exc

    try:
        pages, report = json.loads(result.stdout)
    except ValueError as exc:  # JSONDecodeError, or not a 2-item result
        raise AnalysisCrashed("worker returned no valid result") from exc
    return pages, report


class FormsTextRecovery(AddOn):
    """Recovers filled form field text into the page text layer."""

    def select_documents(self):
        """Pick the documents to work on and count them.

        Manual runs act on the selected documents or the search query. When a
        run has neither (a scheduled or standalone run) we search the configured
        project for documents that still need processing, i.e. that are not yet
        tagged with the current TAG_KEY/TAG_VALUE.

        Returns (documents, expected_count, description). `documents` is always
        iterable; `expected_count` can be 0.
        """
        if self.documents or self.query:
            return (
                self.get_documents(),
                self.get_document_count(),
                "the current selection/search",
            )

        project = self.data.get("project")
        if project:
            query = (
                f"project:{project} +status:success "
                f"-data_{TAG_KEY}:{TAG_VALUE} "
                f"-data_{TAG_KEY}:{FAILED_VALUE} "
                f'+data_ocr_form_problem:"true" sort:created_at'
            )
            print(f"Searching for unprocessed documents: {query}")
            results = self.client.documents.search(query)
            return iter(results), results.count, f"project {project} (unprocessed)"

        return iter(()), 0, "nothing"

    def time_exceeded(self):
        """True once the run has been going for longer than the time limit."""
        return (
            bool(self.time_limit)
            and (time.monotonic() - self.start) > self.time_limit * 60
        )

    def fetch_pdf(self, document):
        """Download the original PDF."""
        content = document.pdf
        if not content.startswith(b"%PDF"):
            raise ValueError(
                f"{len(content)} bytes from {document.get_pdf_url()} are not a PDF"
            )
        return content

    def page_batches(self, pages):
        """Group the selected pages: a payload carrying positions is rejected
        unless its page numbers are consecutive.
        """
        ordered = sorted(pages, key=lambda item: item["page_number"])

        runs = []
        for page in ordered:
            payload = {key: page[key] for key in ("page_number", "text", "positions")}
            if runs and payload["page_number"] == runs[-1][-1]["page_number"] + 1:
                runs[-1].append(payload)
            else:
                runs.append([payload])

        return [
            run[start : start + PAGE_CHUNK_SIZE]
            for run in runs
            for start in range(0, len(run), PAGE_CHUNK_SIZE)
        ]

    def wait_for(self, attempt, max_wait, retry_every=RETRY_EVERY):
        """Retry `attempt` every `retry_every` seconds, for up to `max_wait`.

        `attempt` returns True once it has succeeded and False to be tried
        again.
        """
        # Monotonic, because a batch can run for hours.
        deadline = time.monotonic() + max_wait
        while True:
            if attempt():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(retry_every)

    def upload_pages(self, document, pages):
        """Write the new page text, in chunks, waiting out reprocessing."""

        def send(chunk):
            try:
                resp = self.client.patch(
                    f"documents/{document.id}/", json={"pages": chunk}
                )
                resp.raise_for_status()
            except APIError as exc:
                if "processing" in str(exc):
                    print("Document is still processing, retrying...")
                    return False
                print(f"Unexpected error: {exc}. Exiting retries.")
                raise
            return True

        for chunk in self.page_batches(pages):
            first = chunk[0]["page_number"]
            last = chunk[-1]["page_number"]
            print(f"Updating page text (pages {first} to {last})")

            if not self.wait_for(
                lambda chunk=chunk: send(chunk), MAX_WAIT_UPLOAD_PAGES
            ):
                print(
                    f"Failed to update pages {first} to {last}"
                    f" within {MAX_WAIT_UPLOAD_PAGES} seconds."
                )
                self.set_message(
                    "Failed to update page text in a timely manner. "
                    "Please email info@documentcloud.org to debug."
                )
                sys.exit(1)
            print("Completed updating the page text")

    def write_tag(self, document, value):
        """Write TAG_KEY=value on the document, verifying it stuck.

        Returns True once the value is stored, False if it could not be written
        within MAX_WAIT_TAG_DOCUMENT seconds.
        """

        def send():
            try:
                self.client.put(
                    f"documents/{document.id}/data/{TAG_KEY}/",
                    json={"values": [value]},
                )
                resp = self.client.get(f"documents/{document.id}/")
                resp.raise_for_status()
            except APIError as exc:
                print(f"Could not write {TAG_KEY}:{value}: {exc}. Retrying...")
                return False

            # A 200 is not enough: a concurrent full save() of a document read
            # before the tag writes the stale `data` back over it.
            stored = resp.json().get("data", {}).get(TAG_KEY)
            if stored != [value]:
                print(f"Tag did not stick (found {stored!r}). Retrying...")
                return False
            return True

        return self.wait_for(send, MAX_WAIT_TAG_DOCUMENT)

    def tag_document(self, document):
        """Record that this document has been processed."""
        print("Tagging document...")
        if not self.write_tag(document, TAG_VALUE):
            print(f"Failed to tag document within {MAX_WAIT_TAG_DOCUMENT} seconds.")
            self.set_message(
                "Failed to set the tag for this document. "
                "Email info@documentcloud.org to debug."
            )
            sys.exit(1)
        print("Finished tagging document")

    def quarantine_document(self, document):
        """Mark a document whose analysis crashed, so later runs skip it."""
        print("Quarantining document (analysis crashed)...")
        if not self.write_tag(document, FAILED_VALUE):
            print(
                f"Failed to quarantine document within {MAX_WAIT_TAG_DOCUMENT} seconds."
            )
            self.set_message(
                "Failed to quarantine a document that crashed analysis. "
                "Email info@documentcloud.org to debug."
            )
            sys.exit(1)
        print(f"Quarantined document (tagged {TAG_KEY}:{FAILED_VALUE})")

    def upload_dry_run(self):
        """Attach the dry run output to this Add-On run, then delete it."""
        path = self.report.close()
        if path is None:
            return

        try:
            with open(path, "rb") as handle:
                self.upload_file(handle)
        finally:
            os.remove(path)

    def main(self):
        """Work through the documents that need processing."""
        self.client.session.headers.update({"User-Agent": USER_AGENT})
        self.start = time.monotonic()

        dry_run = self.data.get("dry_run", False)
        limit = self.data.get("max_documents") or 0
        self.time_limit = self.data.get("time_limit") or 0

        origin = "scheduled" if self.event_id else "manual"
        print(
            f"Options: dry_run={dry_run}, max_documents={limit}, "
            f"time_limit={self.time_limit} min ({origin} run)"
        )

        if dry_run:
            print("DRY RUN: nothing will be written.")

        documents, expected, source = self.select_documents()
        if not expected:
            if self.documents or self.query:
                message = (
                    "It looks like no documents were selected. Search for some "
                    "or select them and run again."
                )
            elif self.data.get("project"):
                message = (
                    f"Nothing to do: every document in project "
                    f"{self.data['project']} is already processed "
                    f"(tagged {TAG_KEY}:{TAG_VALUE})."
                )
            elif self.event_id:
                message = (
                    "This scheduled run has no Project ID set. Add a Project ID "
                    "to the schedule so the add-on knows which documents to "
                    "process."
                )
            else:
                message = (
                    "No documents selected, no search query, and no Project ID "
                    "set. Select documents, run a search, or set a project."
                )
            print(message)
            self.set_message(message)
            sys.exit(0)

        print(f"Targeting {source}: {expected} document(s).")
        if limit > 0:
            print(f"Processing at most {limit} documents this run.")
            documents = itertools.islice(documents, limit)
            expected = min(expected, limit)

        self.report = Archive(self.id, {TAG_KEY: [TAG_VALUE]})
        try:
            processed, recovered, tagged, touched = self.process(
                documents, dry_run, expected
            )

            counts = [f"{processed} documents", f"{recovered} field values recovered"]
            if not dry_run:
                counts.append(f"{tagged} tagged")
            summary = f"Finished: {', '.join(counts)}."
            print(summary)

            if touched:
                print(
                    f"Touched {len(touched)} document(s): "
                    f"{', '.join(str(doc_id) for doc_id in touched)}"
                )

            if dry_run and processed:
                self.upload_dry_run()
                self.set_message(
                    "Dry run complete. Nothing was written; the report of what "
                    "would have changed is attached to this run."
                )
            else:
                self.set_message(summary)
        finally:
            self.report.discard()

    def process(self, documents, dry_run, expected):
        """Work through the documents."""
        processed = 0
        recovered = 0
        tagged = 0
        touched = []

        for document in documents:
            if self.time_exceeded():
                print(
                    f"Time limit ({self.time_limit} min) reached; stopping. "
                    "Remaining documents will be processed on the next run."
                )
                break

            if document.status == "error":
                print(f"{document.id}  is in an error state; skipping it.")
                continue

            try:
                pdf_bytes = self.fetch_pdf(document)
            except (requests.RequestException, APIError, ValueError) as exc:
                print(f"{document.id}  could not download the PDF: {exc}")
                continue

            try:
                pages, report = analyze_isolated(pdf_bytes, keep_diff=dry_run)
            except AnalysisCrashed as exc:  # native abort, timeout, or bad output
                print(f"{document.id}  analysis failed ({exc}); skipping.")
                if not dry_run:
                    self.quarantine_document(document)
                continue

            fields = sum(item.get("fields_recovered", 0) for item in report)
            skipped = sum(1 for item in report if item["action"] == "skip")
            methods = Counter()
            for item in report:
                methods.update(item.get("position_methods", {}))
            placed = ", ".join(
                f"{count} {label}" for label, count in sorted(methods.items())
            )
            print(
                f"{document.id}  {len(report)} pages | {len(pages)} selected | "
                f"{skipped} skipped | {fields} fields recovered"
                + (f" | boxes: {placed}" if placed else "")
            )

            processed += 1
            recovered += fields

            if dry_run:
                self.report.add(document, pages, report)
            else:
                if pages:
                    self.upload_pages(document, pages)
                self.tag_document(document)
                tagged += 1
                touched.append(document.id)
                print(
                    f"Done {document.id}: "
                    f"{'wrote page text, ' if pages else ''}"
                    f"tagged {TAG_KEY}:{TAG_VALUE}"
                )

            if expected:
                self.set_progress(min(100, round(100 * processed / expected)))

        return processed, recovered, tagged, touched


if __name__ == "__main__":
    FormsTextRecovery().main()
