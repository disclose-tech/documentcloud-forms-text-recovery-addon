"""
Recovers texts inside PDF form fields.

This Add-On downloads the PDF, reads the values out of the form, and writes
them into the page text along with their estimated word positions.
"""

import itertools
import os
import sys
import time
from collections import Counter

import requests
from documentcloud.addon import AddOn
from documentcloud.exceptions import APIError

import recover
from dry_run import Archive

TAG_KEY = "forms_text_recovery_addon"
TAG_VALUE = "v1"

USER_AGENT = "Disclose Forms Text Recovery Add-On"

PAGE_CHUNK_SIZE = 20

# seconds
RETRY_EVERY = 5
MAX_WAIT_UPLOAD_PAGES = 300
MAX_WAIT_TAG_DOCUMENT = 60


class FormsTextRecovery(AddOn):
    """Recovers filled form field text into the page text layer."""

    def validate(self):
        """Validate that we can run."""
        self.document_count = self.get_document_count()
        if not self.document_count:
            self.set_message(
                "It looks like no documents were selected. Search for some or "
                "select them and run again."
            )
            sys.exit(0)
        return True

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

            if not self.wait_for(lambda: send(chunk), MAX_WAIT_UPLOAD_PAGES):
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

    def tag_document(self, document):
        """Record that this document has been processed."""

        def send():
            try:
                self.client.put(
                    f"documents/{document.id}/data/{TAG_KEY}/",
                    json={"values": [TAG_VALUE]},
                )
                resp = self.client.get(f"documents/{document.id}/")
                resp.raise_for_status()
            except APIError as exc:
                print(f"Could not tag document: {exc}. Retrying...")
                return False

            # A 200 is not enough: a concurrent full save() of a document read
            # before the tag writes the stale `data` back over it.
            stored = resp.json().get("data", {}).get(TAG_KEY)
            if stored != [TAG_VALUE]:
                print(f"Tag did not stick (found {stored!r}). Retrying...")
                return False
            return True

        print("Tagging document...")
        if not self.wait_for(send, MAX_WAIT_TAG_DOCUMENT):
            print(f"Failed to tag document within {MAX_WAIT_TAG_DOCUMENT} seconds.")
            self.set_message(
                "Failed to set the tag for this document. "
                "Email info@documentcloud.org to debug."
            )
            sys.exit(1)
        print("Finished tagging document")

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
        """Work through the selected documents."""
        self.client.session.headers.update({"User-Agent": USER_AGENT})

        if not self.validate():
            sys.exit(0)

        dry_run = self.data.get("dry_run", False)
        to_tag = self.data.get("to_tag", True)
        limit = self.data.get("max_documents") or 0

        # The workflow masks every line of the dispatched payload, so this is
        # the only place a run can say what it was asked to do.
        print(f"Options: dry_run={dry_run}, to_tag={to_tag}, max_documents={limit}")

        if dry_run:
            print("DRY RUN: nothing will be written.")

        self.report = Archive(self.id, {TAG_KEY: [TAG_VALUE]})

        documents = self.get_documents()
        expected = self.document_count
        if limit > 0:
            print(f"Processing at most {limit} documents this run.")
            documents = itertools.islice(documents, limit)
            expected = min(expected, limit)

        try:
            processed, recovered, tagged = self.process(
                documents, dry_run, to_tag, expected
            )

            counts = [f"{processed} documents", f"{recovered} field values recovered"]
            if not dry_run:
                counts.append(f"{tagged} tagged")
            summary = f"Finished: {', '.join(counts)}."
            print(summary)

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

    def process(self, documents, dry_run, to_tag, expected):
        """Work through the documents."""
        processed = 0
        recovered = 0
        tagged = 0

        for document in documents:
            if document.status == "error":
                print(f"{document.id}  is in an error state; skipping it.")
                continue

            try:
                pdf_bytes = self.fetch_pdf(document)
            except (requests.RequestException, APIError, ValueError) as exc:
                print(f"{document.id}  could not download the PDF: {exc}")
                continue

            pages, report = recover.analyze(pdf_bytes, keep_diff=dry_run)

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
                if to_tag:
                    self.tag_document(document)
                    tagged += 1
                else:
                    print("Not tagging: to_tag is off for this run.")

            if expected:
                self.set_progress(min(100, round(100 * processed / expected)))

        return processed, recovered, tagged


if __name__ == "__main__":
    FormsTextRecovery().main()
