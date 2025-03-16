import sys
import asyncio
import time
import re
from typing import List, Tuple, Optional, Dict

from playwright.async_api import async_playwright
from PyQt5.QtCore import QThread, pyqtSignal, pyqtSlot, Qt
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QTextEdit, QStatusBar, QProgressBar, QSizePolicy
)
from PyQt5.QtGui import QIcon  # Import QIcon

# Global configuration
CONCURRENT_LIMIT = 20
search_cache: Dict[str, str] = {}

def is_subsequence(small: List[str], big: List[str]) -> bool:
    """Return True if all words in 'small' appear in 'big' in order (not necessarily consecutively)."""
    it = iter(big)
    return all(word in it for word in it)

def normalize_text(text: str) -> str:
    """Normalize text: lowercase, remove extra whitespace, hyphens, commas, and various Unicode whitespace."""
    text = text.replace('-', '').replace(',', '') # Remove hyphens and commas
    text = re.sub(r'\s+', ' ', text) # Replace multiple whitespace with single space
    text = text.strip() # Strip leading/trailing whitespace
    return text.lower()

# Custom QPlainTextEdit that emits signals on Enter and Escape.
class SearchTextEdit(QPlainTextEdit):
    enterPressed = pyqtSignal()
    escapePressed = pyqtSignal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() == Qt.ControlModifier:
                super().keyPressEvent(event)
            else:
                event.accept()
                self.enterPressed.emit()
        elif event.key() == Qt.Key_Escape:
            event.accept()
            self.escapePressed.emit()
        else:
            super().keyPressEvent(event)

# Wait for the page to update its results.
async def wait_for_results_update(page) -> None:
    await page.wait_for_function(
        "document.querySelector('span.page-results') && document.querySelector('span.page-results').textContent.trim() !== ''",
        timeout=0
    )

# Perform a binary search on the search term to find the longest matching prefix.
async def binary_search_partial(term: str, page, base_url: str, cancel_event: asyncio.Event) -> Optional[str]:
    words = term.split()
    lo, hi = 1, len(words)
    best: Optional[str] = None
    while lo <= hi:
        if cancel_event.is_set():
            return None
        mid = (lo + hi) // 2
        prefix = " ".join(words[:mid])
        await page.goto(base_url, wait_until="networkidle", timeout=0)
        await page.wait_for_selector("div.main-search input.search-term", timeout=30000)
        await page.fill("div.main-search input.search-term", prefix)
        await page.press("div.main-search input.search-term", "Enter")
        try:
            await wait_for_results_update(page)
        except asyncio.TimeoutError:
            partial_content = ""
        else:
            partial_content = (await page.text_content("span.page-results")) or ""
        if partial_content and "Displaying" in partial_content:
            best = prefix # Keep track of the best prefix so far
            lo = mid + 1   # Try to find an even longer prefix
        else:
            hi = mid - 1   # Shorten the prefix if no results
    return best

# Modify the search_term function to better handle partial matches
async def search_term(term: str, base_url: str, context, cancel_event: asyncio.Event, semaphore: asyncio.Semaphore) -> Tuple[str, str]:
    if cancel_event.is_set():
        return term, "Cancelled"
    if term in search_cache:
        return term, search_cache[term]
    async with semaphore:
        page = await context.new_page()
        try:
            await page.goto(base_url, wait_until="networkidle", timeout=0)
            await page.wait_for_selector("div.main-search input.search-term", timeout=30000)
            await page.fill("div.main-search input.search-term", term)
            await page.press("div.main-search input.search-term", "Enter")
            try:
                await wait_for_results_update(page)
            except asyncio.TimeoutError:
                content = ""
            else:
                content = (await page.text_content("span.page-results")) or ""

            initial_result_type = ""
            full_match_prefix = "Displaying search results for:"

            partial: Optional[str] = None

            if content and full_match_prefix in content:
                displayed_term_match = re.search(rf"{re.escape(full_match_prefix)}\s*\"(.+?)\"", content)
                if displayed_term_match:
                    displayed_term = displayed_term_match.group(1).strip()
                    if normalize_text(term) == normalize_text(displayed_term):
                        initial_result_type = "full_match_prefix"
                    else:
                        initial_result_type = "larger_description_prefix"
                else:
                    initial_result_type = "larger_description_prefix_fail"
            elif content and "Displaying" in content:
                initial_result_type = "larger_description_general"
            else:
                # Here we'll check if we should try a partial search
                # First, try the binary search to find the longest matching prefix
                partial = await binary_search_partial(term, page, base_url, cancel_event)

                # If we found a partial match, let's immediately check if it's part of a larger description
                if partial:
                    # Navigate to search with the partial term
                    await page.goto(base_url, wait_until="networkidle", timeout=0)
                    await page.wait_for_selector("div.main-search input.search-term", timeout=30000)
                    await page.fill("div.main-search input.search-term", partial)
                    await page.press("div.main-search input.search-term", "Enter")
                    try:
                        await wait_for_results_update(page)
                    except asyncio.TimeoutError:
                        pass

                    # Check if the partial term appears within a larger description template
                    description_cells = await page.query_selector_all("td[data-column='description']")

                    # Flag to track if we found it in a template
                    found_in_template = False
                    template_text = ""
                    template_id = "Not found"

                    # Normalize the partial for comparison
                    normalized_partial = normalize_text(partial)
                    partial_words = normalized_partial.split()

                    for cell in description_cells:
                        cell_text = (await cell.text_content()).strip()
                        normalized_cell = normalize_text(cell_text)
                        cell_words = normalized_cell.split()

                        # Check if partial is a subsequence in the description
                        if is_subsequence(partial_words, cell_words):
                            found_in_template = True
                            template_text = cell_text

                            # Try to get Term ID
                            parent_row = await cell.evaluate_handle("node => node.parentElement")
                            id_element = await parent_row.query_selector("a.view-record")
                            if id_element:
                                template_id = (await id_element.text_content()).strip()

                            break

                    if found_in_template:
                        # Override the result type if we found it in a template
                        initial_result_type = "template_match"
                        description_text = template_text
                        term_id_number = template_id
                    else:
                        initial_result_type = "partial"
                else:
                    initial_result_type = "no_match"

            description_text = "Not found"
            term_id_number = "Not found"
            is_deleted_description = False
            found_full_description_match = False
            found_in_description = False

            # Only process standard result checks if we haven't already identified a template match
            if initial_result_type != "template_match":
                if initial_result_type != "no_match":
                    view_record_link = await page.query_selector("a.view-record")
                    if view_record_link:
                        term_id_number = (await view_record_link.text_content()).strip()

                    description_cells = await page.query_selector_all("td[data-column='description']")

                    matched_cell_text = ""

                    for cell in description_cells:
                        cell_text = (await cell.text_content()).strip()
                        normalized_cell_text = normalize_text(cell_text)
                        normalized_term = normalize_text(term)

                        if normalized_term == normalized_cell_text:
                            found_full_description_match = True
                            found_in_description = True
                            parent_row = await cell.evaluate_handle("node => node.parentElement")
                            notes_element = await parent_row.query_selector("td[data-column='notes']")
                            if notes_element:
                                notes_text = (await notes_element.text_content()).strip()
                                if re.search(r"deleted", normalize_text(notes_text)):
                                    is_deleted_description = True
                                    matched_cell_text = cell_text
                                    break
                            matched_cell_text = cell_text
                            break

                        elif normalized_term in normalized_cell_text:
                            found_in_description = True
                            parent_row = await cell.evaluate_handle("node => node.parentElement")
                            notes_element = await parent_row.query_selector("td[data-column='notes']")
                            if notes_element:
                                notes_text = (await notes_element.text_content()).strip()
                                if re.search(r"deleted", normalize_text(notes_text)):
                                    is_deleted_description = True
                                    matched_cell_text = cell_text
                                    break
                            matched_cell_text = cell_text
                            if found_full_description_match:
                                break

                    # Check if we should look for partial matches in descriptions
                    if not found_in_description and initial_result_type == "partial" and partial:
                        normalized_partial_words = normalize_text(partial).split()
                        for cell in description_cells:
                            cell_text = (await cell.text_content()).strip()
                            normalized_cell_words = normalize_text(cell_text).split()
                            if is_subsequence(normalized_partial_words, normalized_cell_words):
                                found_in_description = True
                                description_text = cell_text
                                break

            # Determine the final result based on all our checks
            if initial_result_type == "template_match":
                result = f"Apart of a larger description (Example - {description_text} - Term ID: {term_id_number})"
            elif is_deleted_description:
                result = f"Deleted description found (Term ID: {term_id_number})"
            elif found_full_description_match:
                result = f"Full match found (Term ID: {term_id_number})"
            elif found_in_description:
                description_element = await page.query_selector("td[data-column='description']")
                if description_element:
                    dt = await description_element.text_content()
                    if dt:
                        description_text = dt.strip()
                result = f"Apart of a larger description (Example - {description_text} - Term ID: {term_id_number})"
            elif initial_result_type == "partial" and partial:
                result = f"Full match not found, but partial match found: '{partial}' (Term ID: {term_id_number})"
            elif initial_result_type == "no_match":
                result = "No match found"
            elif view_record_link:
                description_element = await page.query_selector("td[data-column='description']")
                if description_element:
                    dt = await description_element.text_content()
                    if dt:
                        description_text = dt.strip()
                result = f"Apart of a larger description (Example - {description_text} - Term ID: {term_id_number})"
            else:
                result = "Apart of a larger description (Example - Description not found - Term ID: Not found)"

            search_cache[term] = result
            return term, result
        finally:
            await page.close()

# QThread subclass to run searches in a background thread.
class SearchWorker(QThread):
    result_signal = pyqtSignal(tuple) # Signal for individual result (term, result)
    error_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int)

    def __init__(self, description: str, parent=None):
        super().__init__(parent)
        self.description = description
        self.base_url = "https://idm-tmng.uspto.gov/id-master-list-public.html"
        self._cancel_event: Optional[asyncio.Event] = None
        self._tasks: List[asyncio.Task] = []
        self._semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
        self.terms: List[str] = [] # Store terms to calculate progress correctly

    async def _run_searches(self, terms: List[str], progress_callback):
        self._cancel_event = asyncio.Event()
        self.terms = terms # Store terms for progress calculation
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()

            tasks = []
            for term in terms:
                task = asyncio.create_task(search_term(term, self.base_url, context, self._cancel_event, self._semaphore))
                tasks.append(task)

            completed_count = 0
            for task in asyncio.as_completed(tasks):
                try:
                    term, result = await task
                    self.result_signal.emit((term, result)) # Emit individual result
                except asyncio.CancelledError:
                    term = "Cancelled"
                    result = "Cancelled"
                    self.result_signal.emit((term, result)) # Emit cancellation result too
                except Exception as e:
                    self.error_signal.emit(str(e)) # Emit error signal, but continue processing other terms if possible
                    continue # Continue to next term if one fails

                completed_count += 1
                progress_callback(int((completed_count / len(self.terms)) * 100)) # Update progress

            await context.close()
            await browser.close()
            return # No need to return all results at once anymore

    def run(self):
        terms = [term.strip() for term in self.description.split(';') if term.strip()]
        start_time = time.time()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        def progress_update(p):
            self.progress_signal.emit(int(p))

        try:
            loop.run_until_complete(self._run_searches(terms, progress_update))
            elapsed_time = time.time() - start_time
            self.result_signal.emit(("Search time", f"{elapsed_time:.2f} seconds")) # Emit search time as a special result
        except Exception as e:
            self.error_signal.emit(str(e))
        finally:
            loop.close()

    def cancel(self):
        if self._cancel_event:
            self._cancel_event.set()
        for task in self._tasks:
            task.cancel()

# Main GUI window.
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("USPTO Search Checker")
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry()
        self.resize(available.width() // 2, available.height() // 2)
        self.setup_ui()
        self.setStyleSheet(self.stylesheet())
        self.results_dict: Dict[str, str] = {} # Store results for final categorized output

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        self.label = QLabel("Enter search terms (separated by semicolon):")
        layout.addWidget(self.label)

        self.entry = SearchTextEdit()
        self.entry.setPlaceholderText("Paste or type search terms here...")
        self.entry.setMinimumHeight(80)
        self.entry.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.entry)

        self.hint_label = QLabel("Hint: Press Enter to search, Esc to cancel search, Ctrl+Enter for a new line.")
        layout.addWidget(self.hint_label)

        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self.run_search)
        layout.addWidget(self.search_button)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel_search)
        self.cancel_button.setEnabled(False)
        layout.addWidget(self.cancel_button)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        layout.addWidget(self.progress_bar)

        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        layout.addWidget(self.output_text)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)

        self.entry.enterPressed.connect(self.run_search)
        self.entry.escapePressed.connect(self.cancel_search)

    def stylesheet(self):
        return """
            QMainWindow { background-color: #f0f0f0; }
            QLabel { font-size: 14px; color: #333; margin-bottom: 5px; }
            QLabel#hint_label { font-size: 12px; color: #666; margin-top: 5px; }
            SearchTextEdit { font-size: 25px; padding: 10px; border: 1px solid #ccc; border-radius: 4px; background-color: #fff; }
            QPushButton { font-size: 25px; padding: 10px 15px; border-radius: 4px; color: #fff; background-color: #5cb85c; border: none; }
            QPushButton:hover { background-color: #449d44; }
            QPushButton:disabled { background-color: #d3d3d3; }
            QProgressBar { border: 1px solid #ccc; border-radius: 4px; text-align: center; height: 20px; }
            QProgressBar::chunk { background-color: #5cb85c; border-radius: 4px; }
            QTextEdit { font-size: 25px; padding: 10px; border: 1px solid #ccc; border-radius: 4px; background-color: #fff; }
            QStatusBar { font-size: 12px; color: #333; }
        """

    def run_search(self):
        description = self.entry.toPlainText().strip()
        if not description:
            self.status_bar.showMessage("Please enter search terms", 5000)
            return
        self.output_text.clear() # Clear previous results
        self.output_text.setPlainText("Searching...\n")
        self.output_text.setStyleSheet("color: #333;")
        self.search_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setValue(0)
        self.results_dict = {} # Clear previous results dictionary

        self.worker = SearchWorker(description)
        self.worker.result_signal.connect(self.update_output) # Connect to individual result signal
        self.worker.error_signal.connect(self.display_error)
        self.worker.progress_signal.connect(self.progress_bar.setValue)
        self.worker.start()

    def cancel_search(self):
        if hasattr(self, 'worker'):
            self.worker.cancel()
            self.status_bar.showMessage("Cancelling search...", 3000)

    @pyqtSlot(tuple)
    def update_output(self, result_tuple):
        term, status = result_tuple
        if term == "Search time":
            self.setWindowTitle(f"USPTO Search Checker - Search time: {status}")
            self.search_button.setEnabled(True)
            self.cancel_button.setEnabled(False)
            self.status_bar.showMessage("Search completed", 5000)
            self.display_final_results() # Display categorized final results
            return # Exit to prevent appending search time to output

        self.results_dict[term] = status # Store individual result

        term_display = f"<b>{term.strip().capitalize()}:</b> "
        if status == "No match found":
            status_display = f"No match found"
            color = "#333"
        elif status.startswith("Full match found"):
            status_display = f"<span style='color: green;'>Full match found</span>"
            color = "#333"
        elif status.startswith("Full match not found, but partial match found"):
            status_display = f"<span style='color: orange;'>{status}</span>"
            color = "#333"
        elif status.startswith("Apart of a larger description"):
            status_display = f"<span style='color: blue;'>{status}</span>"
            color = "#333"
        elif status.startswith("Deleted description found"):
            status_display = f"<span style='color: red; text-decoration: line-through;'>{status}</span>"
            color = "#333"
        elif status == "Cancelled":
            status_display = "<span style='color: grey;'>Cancelled</span>"
            color = "#333"
        else:
            status_display = status # Fallback
            color = "#333"

        current_text = self.output_text.toHtml()
        if "Searching..." in current_text: # Remove "Searching..." message on first result
            current_text = ""

        self.output_text.setHtml(current_text + f"<p style='color: {color}; font-size: 25px;'>{term_display}{status_display}</p>")


    def display_final_results(self):
        def capitalize_term(term_string):
            return term_string.strip().capitalize()

        no_results = []
        partial_results = []
        full_matches = []
        larger_description_results = []
        deleted_descriptions_results = []

        for term, status in self.results_dict.items():
            if status == "No match found":
                no_results.append((term, status))
            elif status.startswith("Full match found"):
                full_matches.append((term, status))
            elif status.startswith("Full match not found, but partial match found"):
                partial_results.append((term, status))
            elif status.startswith("Apart of a larger description"):
                larger_description_results.append((term, status))
            elif status.startswith("Deleted description found"):
                deleted_descriptions_results.append((term, status))

        html_final = """
        <html>
          <head>
            <style>
              body { font-family: Arial, sans-serif; color: #333; }
              h2 { text-align: center; font-weight: bold; margin: 1em 0 0.5em 0; font-size: 1.3em; color: #2c3e50; text-transform: capitalize; }
              ol { list-style-type: decimal; padding-left: 1.2em; }
              li { margin-bottom: 0.6em; font-size: 1em; }
              .partial-prefix { color: #c0392b; font-weight: bold; }
              .partial-text { color: #3498db; font-style: italic; }
              .result-term { font-weight: bold; }
              .larger-description { color: #e67e22; }
              .deleted-description { text-decoration: line-through; color: #777; }
            </style>
          </head>
          <body>
        """
        if no_results:
            html_final += "<h2>Not on the USPTO</h2><ol>"
            for term, _ in no_results:
                html_final += f"<li><span class='result-term'>{capitalize_term(term)}:</span> No match found</li>"
            html_final += "</ol>"
        if partial_results:
            html_final += "<h2>Partial Results</h2><ol>"
            for term, status in partial_results:
                m = re.search(r"partial match found:\s*'(.+?)'\s*(\(Term ID: (.+?)\))?", status)
                partial_text = m.group(1) if m else ""
                term_id_partial = m.group(3) if m and m.group(3) else "Not found"
                html_final += (
                    f"<li><span class='result-term'>{capitalize_term(term)}:</span> "
                    f"Partial match found for prefix: " # Added explanation
                    f"<span class='partial-prefix'>'{partial_text}'</span> (Term ID: {term_id_partial}). "
                    f"Consider checking broader term for relevance.</li>" # Added explanation
                )
            html_final += "</ol>"
        if larger_description_results:
            html_final += "<h2>Apart of a Larger Description</h2><ol>"
            for term, status in larger_description_results:
                m = re.search(r"Apart of a larger description \(Example - (.+?) - Term ID: (.+?)\)", status)
                description_text = m.group(1) if m else "Description not found"
                term_id_larger = m.group(2) if m else "Not found"
                html_final += f"<li><span class='result-term'>{capitalize_term(term)}:</span> Apart of a larger description (Example - <span class='larger-description'>{description_text}</span> - Term ID: {term_id_larger})</li>"
            html_final += "</ol>"
        if deleted_descriptions_results:
            html_final += "<h2>Deleted Descriptions</h2><ol>"
            for term, status in deleted_descriptions_results:
                m = re.search(r"Deleted description found \(Term ID: (.+?)\)", status)
                term_id_deleted = m.group(1) if m else "Not found"
                html_final += f"<li><span class='result-term'>{capitalize_term(term)}:</span> <span class='deleted-description'>{term}</span> (Term ID: {term_id_deleted})</li>"
            html_final += "</ol>"
        if full_matches:
            html_final += "<h2>Full Match Found</h2><ol>"
            for term, status in full_matches:
                m = re.search(r"Full match found \(Term ID: (.+?)\)", status)
                term_id_full = m.group(1) if m else "Not found"
                html_final += f"<li><span class='result-term'>{capitalize_term(term)}:</span> Full match found (Term ID: {term_id_full})</li>"
            html_final += "</ol>"
        html_final += "</body></html>"
        self.output_text.setHtml(html_final) # Set the categorized HTML output

    def display_error(self, msg):
        self.output_text.clear()
        self.output_text.setStyleSheet("color: #dc3545;")
        self.output_text.setHtml(f"<b>Error:</b> <pre style='font-family: monospace;'>{msg}</pre>")
        self.search_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.status_bar.showMessage("Search failed", 5000)

def main():
    app = QApplication(sys.argv)

    # Set application icon
    app_icon = QIcon(r"C:\Users\Chevroy\Documents\meow.ico")
    app.setWindowIcon(app_icon)

    window = MainWindow()
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()