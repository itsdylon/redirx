import { describe, it, expect, vi } from 'vitest';
import * as XLSX from 'xlsx';
import {
  validateUrl,
  validateEmail,
  validateFile,
  validateCSV,
  calculatePasswordStrength,
  type FileValidationResult,
} from './validation';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Create a plain‑text File (CSV or TXT) */
function textFile(content: string, name: string): File {
  return new File([content], name, { type: 'text/plain' });
}

/** Create an XLSX File from an array-of-arrays */
function xlsxFile(data: (string | number)[][], name = 'test.xlsx', sheetNames?: string[]): File {
  const wb = XLSX.utils.book_new();
  if (sheetNames && sheetNames.length > 1) {
    for (let i = 0; i < sheetNames.length; i++) {
      const ws = XLSX.utils.aoa_to_sheet(i === 0 ? data : [['placeholder']]);
      XLSX.utils.book_append_sheet(wb, ws, sheetNames[i]);
    }
  } else {
    const ws = XLSX.utils.aoa_to_sheet(data);
    XLSX.utils.book_append_sheet(wb, ws, sheetNames?.[0] ?? 'Sheet1');
  }
  const buf = XLSX.write(wb, { type: 'array', bookType: 'xlsx' });
  return new File([buf], name, {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  });
}

/** Create an XML sitemap string */
function sitemapXml(urls: string[]): string {
  const locs = urls.map((u) => `<url><loc>${u}</loc></url>`).join('\n');
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${locs}\n</urlset>`;
}

/** Create an XML File */
function xmlFile(content: string, name = 'sitemap.xml'): File {
  return new File([content], name, { type: 'application/xml' });
}

// ===========================================================================
// validateUrl
// ===========================================================================
describe('validateUrl', () => {
  describe('valid relative paths', () => {
    const cases = [
      { url: '/', desc: 'root path' },
      { url: '/about', desc: 'simple path' },
      { url: '/products/item-123', desc: 'nested path with hyphen' },
      { url: '/api/v2/users', desc: 'deep nested path' },
      { url: '/search?query=test&page=1', desc: 'path with query parameters' },
      { url: '/page#section', desc: 'path with fragment' },
      { url: '/path/with_underscore', desc: 'path with underscore' },
      { url: '/path/with.dot', desc: 'path with dot' },
      { url: '/path/~user', desc: 'path with tilde' },
      { url: '/path%20encoded', desc: 'path with URL encoding' },
    ];

    cases.forEach(({ url, desc }) => {
      it(`accepts ${desc}: ${url}`, () => {
        expect(validateUrl(url).valid).toBe(true);
      });
    });
  });

  describe('valid absolute URLs', () => {
    const cases = [
      { url: 'https://example.com', desc: 'HTTPS no path' },
      { url: 'http://example.com', desc: 'HTTP no path' },
      { url: 'https://example.com/path', desc: 'HTTPS with path' },
      { url: 'https://sub.example.com/path', desc: 'with subdomain' },
      { url: 'https://example.com:8080/path', desc: 'with port' },
      { url: 'https://example.com/path?query=value', desc: 'with query' },
    ];

    cases.forEach(({ url, desc }) => {
      it(`accepts ${desc}: ${url}`, () => {
        expect(validateUrl(url).valid).toBe(true);
      });
    });
  });

  describe('invalid URLs', () => {
    const cases = [
      { url: '', desc: 'empty string' },
      { url: '   ', desc: 'whitespace only' },
      { url: ' /path', desc: 'leading whitespace' },
      { url: '/path ', desc: 'trailing whitespace' },
      { url: '/path//double', desc: 'double slashes' },
      { url: 'example.com', desc: 'missing protocol' },
      { url: 'ftp://example.com', desc: 'ftp protocol' },
      { url: 'not-a-url', desc: 'plain text' },
      { url: '\\path', desc: 'backslash path' },
    ];

    cases.forEach(({ url, desc }) => {
      it(`rejects ${desc}: "${url}"`, () => {
        const result = validateUrl(url);
        expect(result.valid).toBe(false);
        expect(result.error).toBeTruthy();
      });
    });
  });
});

// ===========================================================================
// validateEmail
// ===========================================================================
describe('validateEmail', () => {
  describe('valid emails', () => {
    it.each([
      'user@example.com',
      'test.user@example.com',
      'user+tag@example.com',
      'user@sub.example.com',
    ])('accepts %s', (email) => {
      expect(validateEmail(email).valid).toBe(true);
    });
  });

  describe('invalid emails', () => {
    it('rejects empty string', () => {
      expect(validateEmail('').valid).toBe(false);
    });

    it('rejects whitespace only', () => {
      expect(validateEmail('   ').valid).toBe(false);
    });

    it('rejects missing @ symbol', () => {
      const r = validateEmail('userexample.com');
      expect(r.valid).toBe(false);
      expect(r.error).toContain('@');
    });

    it('rejects multiple @ symbols', () => {
      const r = validateEmail('user@@example.com');
      expect(r.valid).toBe(false);
      expect(r.error).toContain('multiple @');
    });

    it('rejects missing domain', () => {
      const r = validateEmail('user@');
      expect(r.valid).toBe(false);
    });

    it('rejects missing TLD', () => {
      const r = validateEmail('user@example');
      expect(r.valid).toBe(false);
      expect(r.error).toContain('domain');
    });

    it('rejects spaces in email', () => {
      const r = validateEmail('user @example.com');
      expect(r.valid).toBe(false);
      expect(r.error).toContain('spaces');
    });
  });
});

// ===========================================================================
// validateFile — CSV / TXT (existing behaviour regression)
// ===========================================================================
describe('validateFile — CSV/TXT', () => {
  it('accepts a valid CSV with URLs', async () => {
    const file = textFile(
      'https://example.com/page1\nhttps://example.com/page2\nhttps://example.com/page3\n',
      'urls.csv'
    );
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    expect(r.rowCount).toBe(3);
    expect(r.errors).toHaveLength(0);
  });

  it('accepts a valid TXT file with URLs', async () => {
    const file = textFile('/about\n/contact\n/products\n', 'urls.txt');
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    expect(r.rowCount).toBe(3);
  });

  it('accepts a multi-column CSV and counts rows', async () => {
    const file = textFile(
      'https://example.com/page1,Title 1\nhttps://example.com/page2,Title 2\n',
      'urls.csv'
    );
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    expect(r.rowCount).toBe(2);
  });

  it('rejects unsupported extension', async () => {
    const file = textFile('https://example.com', 'urls.json');
    const r = await validateFile(file);

    expect(r.valid).toBe(false);
    expect(r.errors[0]).toContain('.csv, .txt, .xml, or .xlsx');
  });

  it('rejects empty file', async () => {
    const file = textFile('', 'empty.csv');
    // Override size to 0
    Object.defineProperty(file, 'size', { value: 0 });
    const r = await validateFile(file);

    expect(r.valid).toBe(false);
    expect(r.errors[0]).toContain('empty');
  });

  it('rejects file exceeding 10MB', async () => {
    const file = textFile('x', 'big.csv');
    Object.defineProperty(file, 'size', { value: 11 * 1024 * 1024 });
    const r = await validateFile(file);

    expect(r.valid).toBe(false);
    expect(r.errors[0]).toContain('exceeds maximum');
  });

  it('warns on file larger than 5MB', async () => {
    const file = textFile('https://example.com/page\n'.repeat(10), 'medium.csv');
    Object.defineProperty(file, 'size', { value: 6 * 1024 * 1024 });
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    expect(r.warnings.some((w) => w.includes('Large file'))).toBe(true);
  });

  it('rejects file with no URL content', async () => {
    const file = textFile('apple\nbanana\ncherry\ndate\nelderberry\nfig\ngrape\nhoneydew\nkiwi\nlemon\nmango\nnectarine\norange\npapaya\nquince\nraspberry\nstrawberry\ntangerine\nugli\nvanilla', 'fruits.csv');
    const r = await validateFile(file);

    expect(r.valid).toBe(false);
    expect(r.errors[0]).toContain('does not appear to contain URLs');
  });

  it('warns when some rows are not URLs', async () => {
    const file = textFile(
      'https://example.com/page1\nnot-a-url\nhttps://example.com/page2\n',
      'mixed.csv'
    );
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    expect(r.warnings.some((w) => w.includes("don't appear to be valid URLs"))).toBe(true);
  });

  it('warns on single URL file', async () => {
    const file = textFile('https://example.com/only\n', 'single.csv');
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    expect(r.warnings.some((w) => w.includes('only 1 URL'))).toBe(true);
  });

  it('warns on large row count (>5000)', async () => {
    const lines = Array.from({ length: 5001 }, (_, i) => `https://example.com/page-${i}`).join('\n');
    const file = textFile(lines, 'large.csv');
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    expect(r.rowCount).toBe(5001);
    expect(r.warnings.some((w) => w.includes('Large dataset'))).toBe(true);
  });

  it('warns on inconsistent column counts in CSV', async () => {
    const file = textFile(
      'https://example.com/page1,title1\nhttps://example.com/page2,title2,extra\nhttps://example.com/page3,title3\n',
      'inconsistent.csv'
    );
    const r = await validateFile(file);

    expect(r.warnings.some((w) => w.includes('inconsistent column counts'))).toBe(true);
  });

  it('rejects file with null bytes', async () => {
    const file = textFile('https://example.com\x00/page', 'null.csv');
    const r = await validateFile(file);

    expect(r.valid).toBe(false);
    expect(r.errors[0]).toContain('invalid characters');
  });

  it('skips header row in URL content check', async () => {
    const file = textFile(
      'url\nhttps://example.com/page1\nhttps://example.com/page2\n',
      'with-header.csv'
    );
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    // Should not warn about "url" not being a URL
    expect(r.warnings.filter((w) => w.includes("don't appear"))).toHaveLength(0);
  });

  it('does not have a convertedFile for CSV/TXT', async () => {
    const file = textFile('https://example.com/page1\nhttps://example.com/page2\n', 'urls.csv');
    const r = await validateFile(file);

    expect(r.convertedFile).toBeUndefined();
  });
});

// ===========================================================================
// validateFile — XML sitemap
// ===========================================================================
describe('validateFile — XML', () => {
  it('validates and converts a valid sitemap', async () => {
    const urls = ['https://example.com/', 'https://example.com/about'];
    const file = xmlFile(sitemapXml(urls));
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    expect(r.rowCount).toBe(2);
    expect(r.errors).toHaveLength(0);
    expect(r.convertedFile).toBeDefined();
    expect(r.convertedFile!.name).toBe('sitemap.txt');
    expect(r.convertedFile!.type).toBe('text/plain');
  });

  it('converted file contains the extracted URLs', async () => {
    const urls = ['https://example.com/page1', 'https://example.com/page2'];
    const file = xmlFile(sitemapXml(urls));
    const r = await validateFile(file);

    const text = await r.convertedFile!.text();
    expect(text).toBe('https://example.com/page1\nhttps://example.com/page2');
  });

  it('rejects sitemap index', async () => {
    const xml = `<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-posts.xml</loc></sitemap>
</sitemapindex>`;
    const file = xmlFile(xml, 'sitemap-index.xml');
    const r = await validateFile(file);

    expect(r.valid).toBe(false);
    expect(r.errors[0]).toContain('sitemap index');
  });

  it('rejects malformed XML', async () => {
    const file = xmlFile('<urlset><url><loc>broken', 'bad.xml');
    const r = await validateFile(file);

    expect(r.valid).toBe(false);
    expect(r.errors[0]).toContain('not valid XML');
  });

  it('rejects non-sitemap XML', async () => {
    const file = xmlFile('<rss><channel></channel></rss>', 'feed.xml');
    const r = await validateFile(file);

    expect(r.valid).toBe(false);
    expect(r.errors[0]).toContain('not a sitemap');
  });

  it('warns on single URL sitemap', async () => {
    const file = xmlFile(sitemapXml(['https://example.com/']), 'single.xml');
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    expect(r.warnings.some((w) => w.includes('only 1 URL'))).toBe(true);
  });

  it('warns on large URL count', async () => {
    const urls = Array.from({ length: 5001 }, (_, i) => `https://example.com/p${i}`);
    const file = xmlFile(sitemapXml(urls), 'large.xml');
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    expect(r.rowCount).toBe(5001);
    expect(r.warnings.some((w) => w.includes('Large dataset'))).toBe(true);
  });

  it('rejects empty XML file', async () => {
    const file = xmlFile('', 'empty.xml');
    Object.defineProperty(file, 'size', { value: 0 });
    const r = await validateFile(file);

    expect(r.valid).toBe(false);
  });

  it('respects the 10MB size limit for XML', async () => {
    const file = xmlFile('x', 'big.xml');
    Object.defineProperty(file, 'size', { value: 11 * 1024 * 1024 });
    const r = await validateFile(file);

    expect(r.valid).toBe(false);
    expect(r.errors[0]).toContain('exceeds maximum');
  });
});

// ===========================================================================
// validateFile — XLSX
// ===========================================================================
describe('validateFile — XLSX', () => {
  it('validates and converts a valid XLSX', async () => {
    const file = xlsxFile([
      ['https://example.com/page1'],
      ['https://example.com/page2'],
    ]);
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    expect(r.rowCount).toBe(2);
    expect(r.errors).toHaveLength(0);
    expect(r.convertedFile).toBeDefined();
    expect(r.convertedFile!.name).toBe('test.txt');
  });

  it('converted file contains the extracted URLs', async () => {
    const file = xlsxFile([
      ['https://example.com/page1'],
      ['https://example.com/page2'],
    ]);
    const r = await validateFile(file);

    const text = await r.convertedFile!.text();
    expect(text).toBe('https://example.com/page1\nhttps://example.com/page2');
  });

  it('skips header row', async () => {
    const file = xlsxFile([
      ['URL'],
      ['https://example.com/page1'],
      ['https://example.com/page2'],
    ]);
    const r = await validateFile(file);

    expect(r.rowCount).toBe(2);
    const text = await r.convertedFile!.text();
    expect(text).not.toContain('URL');
  });

  it('warns on multiple sheets', async () => {
    const file = xlsxFile(
      [['https://example.com/page1']],
      'multi.xlsx',
      ['URLs', 'Other']
    );
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    expect(r.warnings.some((w) => w.includes('2 sheets'))).toBe(true);
  });

  it('rejects XLSX with no URLs', async () => {
    const file = xlsxFile([
      ['Apple'],
      ['Banana'],
      ['Cherry'],
    ]);
    const r = await validateFile(file);

    expect(r.valid).toBe(false);
    expect(r.errors[0]).toContain('does not appear to contain URLs');
  });

  it('rejects empty XLSX sheet', async () => {
    const file = xlsxFile([]);
    const r = await validateFile(file);

    expect(r.valid).toBe(false);
  });

  it('warns on single URL XLSX', async () => {
    const file = xlsxFile([['https://example.com/']]);
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    expect(r.warnings.some((w) => w.includes('only 1 URL'))).toBe(true);
  });

  it('warns on large row count in XLSX', async () => {
    const data = Array.from({ length: 5001 }, (_, i) => [`https://example.com/p${i}`]);
    const file = xlsxFile(data);
    const r = await validateFile(file);

    expect(r.valid).toBe(true);
    expect(r.rowCount).toBe(5001);
    expect(r.warnings.some((w) => w.includes('Large dataset'))).toBe(true);
  });

  it('respects the 10MB size limit for XLSX', async () => {
    const file = xlsxFile([['https://example.com/']]);
    Object.defineProperty(file, 'size', { value: 11 * 1024 * 1024 });
    const r = await validateFile(file);

    expect(r.valid).toBe(false);
    expect(r.errors[0]).toContain('exceeds maximum');
  });
});

// ===========================================================================
// validateCSV alias
// ===========================================================================
describe('validateCSV backwards-compatible alias', () => {
  it('is the same function as validateFile', () => {
    expect(validateCSV).toBe(validateFile);
  });

  it('works correctly when called as validateCSV', async () => {
    const file = textFile('https://example.com/page1\nhttps://example.com/page2\n', 'urls.csv');
    const r = await validateCSV(file);

    expect(r.valid).toBe(true);
    expect(r.rowCount).toBe(2);
  });
});

// ===========================================================================
// Cross-format: extension routing
// ===========================================================================
describe('validateFile — extension routing', () => {
  it('routes .csv to text validation (no convertedFile)', async () => {
    const file = textFile('https://example.com/\n', 'data.csv');
    const r = await validateFile(file);
    expect(r.convertedFile).toBeUndefined();
  });

  it('routes .txt to text validation (no convertedFile)', async () => {
    const file = textFile('https://example.com/\n', 'data.txt');
    const r = await validateFile(file);
    expect(r.convertedFile).toBeUndefined();
  });

  it('routes .xml to XML parser (has convertedFile)', async () => {
    const file = xmlFile(sitemapXml(['https://example.com/']), 'sitemap.xml');
    const r = await validateFile(file);
    expect(r.convertedFile).toBeDefined();
  });

  it('routes .xlsx to XLSX parser (has convertedFile)', async () => {
    const file = xlsxFile([['https://example.com/']]);
    const r = await validateFile(file);
    expect(r.convertedFile).toBeDefined();
  });

  it('rejects .xls (legacy Excel not supported)', async () => {
    const file = new File([''], 'old.xls', { type: 'application/vnd.ms-excel' });
    Object.defineProperty(file, 'size', { value: 100 });
    const r = await validateFile(file);
    expect(r.valid).toBe(false);
    expect(r.errors[0]).toContain('.csv, .txt, .xml, or .xlsx');
  });

  it('rejects .pdf', async () => {
    const file = new File([''], 'report.pdf', { type: 'application/pdf' });
    Object.defineProperty(file, 'size', { value: 100 });
    const r = await validateFile(file);
    expect(r.valid).toBe(false);
  });

  it('rejects .html', async () => {
    const file = new File(['<html></html>'], 'page.html', { type: 'text/html' });
    const r = await validateFile(file);
    expect(r.valid).toBe(false);
  });
});

// ===========================================================================
// calculatePasswordStrength
// ===========================================================================
describe('calculatePasswordStrength', () => {
  it('returns weak for short password', () => {
    const r = calculatePasswordStrength('abc');
    expect(r.strength).toBe('weak');
    expect(r.criteria.minLength).toBe(false);
  });

  it('returns weak when only length is met', () => {
    const r = calculatePasswordStrength('abcdefgh');
    expect(r.strength).toBe('weak');
    expect(r.criteria.minLength).toBe(true);
    expect(r.criteria.hasUppercase).toBe(false);
  });

  it('returns fair for 3 criteria met', () => {
    const r = calculatePasswordStrength('Abcdefgh');
    expect(r.strength).toBe('fair');
    expect(r.score).toBe(3);
  });

  it('returns good for 4 criteria met', () => {
    const r = calculatePasswordStrength('Abcdefg1');
    expect(r.strength).toBe('good');
    expect(r.score).toBe(4);
  });

  it('returns strong for all 5 criteria', () => {
    const r = calculatePasswordStrength('Abcdefg1!');
    expect(r.strength).toBe('strong');
    expect(r.score).toBe(5);
    expect(r.feedback).toHaveLength(0);
  });

  it('provides feedback for missing criteria', () => {
    const r = calculatePasswordStrength('abc');
    expect(r.feedback.length).toBeGreaterThan(0);
    expect(r.feedback.some((f) => f.includes('8 characters'))).toBe(true);
    expect(r.feedback.some((f) => f.includes('uppercase'))).toBe(true);
    expect(r.feedback.some((f) => f.includes('number'))).toBe(true);
    expect(r.feedback.some((f) => f.includes('special'))).toBe(true);
  });
});

describe('file-read cancellation', () => {
  it.each(['csv', 'xml'])('aborts a pending %s FileReader and rejects instead of returning a validation result', async ext => {
    const controller = new AbortController();
    const abort = vi.spyOn(FileReader.prototype, 'abort');
    try {
      const pending = validateFile(new File(['/url'], `cancel.${ext}`), controller.signal);
      controller.abort();
      await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
      expect(abort).toHaveBeenCalledOnce();
    } finally { abort.mockRestore(); }
  });
  it('rejects an already cancelled check without starting a file read', async () => {
    const controller = new AbortController(); controller.abort();
    const read = vi.spyOn(FileReader.prototype, 'readAsText');
    try {
      await expect(validateFile(new File(['/url'], 'cancel.csv'), controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
      expect(read).not.toHaveBeenCalled();
    } finally { read.mockRestore(); }
  });
});
