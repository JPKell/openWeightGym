"""Fixed selectable-PDF parser child, with Linux resource limits before reading untrusted data.

This module never evaluates document instructions, JavaScript or model-generated code.
"""
import io
import json
import sys

MAX_FILE = 1024 * 1024
MAX_TEXT = 200 * 1024
MAX_PAGES = 40


def main():
    """Extract bounded PDF text in a subprocess with memory and CPU limits."""
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    from pypdf import PdfReader
    maximum, text_maximum, page_maximum = map(int, sys.argv[1:]) if len(sys.argv) == 4 else (MAX_FILE, MAX_TEXT, MAX_PAGES)
    try:
        data = sys.stdin.buffer.read(maximum + 1)
        if not data.startswith(b'%PDF-') or len(data) > maximum:
            raise ValueError(f'Use a PDF of at most {maximum // 1024 // 1024} MiB.')
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ValueError('Encrypted PDFs are unsupported. Supply an unencrypted text copy.')
        if not 1 <= len(reader.pages) <= page_maximum:
            raise ValueError(f'PDFs must have one to {page_maximum} pages.')
        pages = []
        total = 0
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            total += len(text.encode('utf-8'))
            if total > text_maximum:
                raise ValueError(f'Extracted PDF text exceeds {text_maximum // 1024} KiB; split the document.')
            if not text.strip():
                raise ValueError('A PDF page has no selectable text. Supply OCR text or remove blank pages; no pages are silently omitted.')
            pages.append({'page': number, 'text': text})
        result = {'pages': pages}
    except ValueError as why:
        result = {'error': str(why)[:300]}
    except Exception:
        result = {'error': 'This PDF could not be parsed. Supply a selectable-text PDF or a UTF-8 text copy.'}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
