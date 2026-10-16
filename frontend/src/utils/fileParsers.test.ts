import { describe, it, expect } from 'vitest';
import * as XLSX from 'xlsx';
import {
  looksLikeUrl,
  parseXmlSitemap,
  parseXlsx,
  urlsToTxtFile,
} from './fileParsers';

// ---------------------------------------------------------------------------
// Helper: create an XLSX File from an array-of-arrays
// ---------------------------------------------------------------------------
function createXlsxFile(
  data: (string | number)[][],
  fileName = 'test.xlsx',
  sheetNames?: string[]
): File {
  const wb = XLSX.utils.book_new();
  if (sheetNames && sheetNames.length > 1) {
    // Multi-sheet workbook
    for (let i = 0; i < sheetNames.length; i++) {
      const sheetData = i === 0 ? data : [['placeholder']];
      const ws = XLSX.utils.aoa_to_sheet(sheetData);
      XLSX.utils.book_append_sheet(wb, ws, sheetNames[i]);
    }
  } else {
    const ws = XLSX.utils.aoa_to_sheet(data);
    XLSX.utils.book_append_sheet(wb, ws, sheetNames?.[0] ?? 'Sheet1');
  }
  const buf = XLSX.write(wb, { type: 'array', bookType: 'xlsx' });
  return new File([buf], fileName, {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  });
}

// ===========================================================================
// looksLikeUrl
// ===========================================================================
describe('looksLikeUrl', () => {
  it('returns true for https URLs', () => {
    expect(looksLikeUrl('https://example.com')).toBe(true);
    expect(looksLikeUrl('https://example.com/path/to/page')).toBe(true);
  });

  it('returns true for http URLs', () => {
    expect(looksLikeUrl('http://example.com')).toBe(true);
    expect(looksLikeUrl('http://example.com:8080/page')).toBe(true);
  });

  it('is case-insensitive for protocol', () => {
    expect(looksLikeUrl('HTTP://EXAMPLE.COM')).toBe(true);
    expect(looksLikeUrl('Https://Example.com')).toBe(true);
  });

  it('returns true for relative paths starting with /', () => {
    expect(looksLikeUrl('/')).toBe(true);
    expect(looksLikeUrl('/about')).toBe(true);
    expect(looksLikeUrl('/products/item-123')).toBe(true);
  });

  it('returns false for empty or falsy values', () => {
    expect(looksLikeUrl('')).toBe(false);
    expect(looksLikeUrl(null as unknown as string)).toBe(false);
    expect(looksLikeUrl(undefined as unknown as string)).toBe(false);
  });

  it('returns false for plain text', () => {
    expect(looksLikeUrl('hello world')).toBe(false);
    expect(looksLikeUrl('example.com')).toBe(false);
    expect(looksLikeUrl('not a url')).toBe(false);
  });

  it('returns false for ftp and other protocols', () => {
    expect(looksLikeUrl('ftp://files.example.com')).toBe(false);
    expect(looksLikeUrl('mailto:user@example.com')).toBe(false);
  });

  it('returns false for strings that look like headers', () => {
    expect(looksLikeUrl('URL')).toBe(false);
    expect(looksLikeUrl('Page')).toBe(false);
    expect(looksLikeUrl('Address')).toBe(false);
  });
});

// ===========================================================================
// parseXmlSitemap
// ===========================================================================
describe('parseXmlSitemap', () => {
  const SITEMAP_NS = 'http://www.sitemaps.org/schemas/sitemap/0.9';

  function makeSitemap(urls: string[], withNamespace = true): string {
    const ns = withNamespace ? ` xmlns="${SITEMAP_NS}"` : '';
    const locs = urls.map((u) => `  <url><loc>${u}</loc></url>`).join('\n');
    return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset${ns}>\n${locs}\n</urlset>`;
  }

  it('extracts URLs from a valid sitemap with namespace', () => {
    const urls = [
      'https://example.com/',
      'https://example.com/about',
      'https://example.com/contact',
    ];
    const result = parseXmlSitemap(makeSitemap(urls, true));

    expect(result.errors).toHaveLength(0);
    expect(result.urls).toEqual(urls);
  });

  it('extracts URLs from a valid sitemap without namespace', () => {
    const urls = ['https://example.com/page1', 'https://example.com/page2'];
    const result = parseXmlSitemap(makeSitemap(urls, false));

    expect(result.errors).toHaveLength(0);
    expect(result.urls).toEqual(urls);
  });

  it('handles sitemap with many URLs', () => {
    const urls = Array.from({ length: 500 }, (_, i) => `https://example.com/page-${i}`);
    const result = parseXmlSitemap(makeSitemap(urls));

    expect(result.errors).toHaveLength(0);
    expect(result.urls).toHaveLength(500);
  });

  it('handles sitemap with extra elements (lastmod, changefreq, priority)', () => {
    const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="${SITEMAP_NS}">
  <url>
    <loc>https://example.com/</loc>
    <lastmod>2024-01-01</lastmod>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://example.com/about</loc>
    <lastmod>2024-01-15</lastmod>
  </url>
</urlset>`;
    const result = parseXmlSitemap(xml);

    expect(result.errors).toHaveLength(0);
    expect(result.urls).toEqual([
      'https://example.com/',
      'https://example.com/about',
    ]);
  });

  it('trims whitespace from loc elements', () => {
    const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="${SITEMAP_NS}">
  <url><loc>  https://example.com/page  </loc></url>
</urlset>`;
    const result = parseXmlSitemap(xml);

    expect(result.urls).toEqual(['https://example.com/page']);
  });

  it('skips empty loc elements but includes others', () => {
    const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="${SITEMAP_NS}">
  <url><loc>https://example.com/page1</loc></url>
  <url><loc>  </loc></url>
  <url><loc></loc></url>
  <url><loc>https://example.com/page2</loc></url>
</urlset>`;
    const result = parseXmlSitemap(xml);

    expect(result.errors).toHaveLength(0);
    expect(result.urls).toEqual([
      'https://example.com/page1',
      'https://example.com/page2',
    ]);
  });

  it('errors on sitemap index files', () => {
    const xml = `<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="${SITEMAP_NS}">
  <sitemap>
    <loc>https://example.com/sitemap-posts.xml</loc>
  </sitemap>
</sitemapindex>`;
    const result = parseXmlSitemap(xml);

    expect(result.errors).toHaveLength(1);
    expect(result.errors[0]).toContain('sitemap index');
    expect(result.urls).toHaveLength(0);
  });

  it('errors on non-sitemap XML (RSS feed)', () => {
    const xml = `<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <item><link>https://example.com/post1</link></item>
  </channel>
</rss>`;
    const result = parseXmlSitemap(xml);

    expect(result.errors).toHaveLength(1);
    expect(result.errors[0]).toContain('not a sitemap');
    expect(result.errors[0]).toContain('<rss>');
  });

  it('errors on non-sitemap XML (HTML document)', () => {
    const xml = `<?xml version="1.0" encoding="UTF-8"?>
<html><body><p>This is HTML</p></body></html>`;
    const result = parseXmlSitemap(xml);

    expect(result.errors).toHaveLength(1);
    expect(result.errors[0]).toContain('not a sitemap');
  });

  it('errors on malformed XML', () => {
    const result = parseXmlSitemap('<urlset><url><loc>unclosed');

    expect(result.errors).toHaveLength(1);
    expect(result.errors[0]).toContain('not valid XML');
  });

  it('errors on completely invalid content', () => {
    const result = parseXmlSitemap('this is not xml at all {{{ }}');

    expect(result.errors).toHaveLength(1);
  });

  it('errors on empty urlset with no loc elements', () => {
    const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="${SITEMAP_NS}">
</urlset>`;
    const result = parseXmlSitemap(xml);

    expect(result.errors).toHaveLength(1);
    expect(result.errors[0]).toContain('no <loc> elements');
  });

  it('errors when all loc elements are empty', () => {
    const xml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="${SITEMAP_NS}">
  <url><loc></loc></url>
  <url><loc>  </loc></url>
</urlset>`;
    const result = parseXmlSitemap(xml);

    expect(result.errors).toHaveLength(1);
    expect(result.errors[0]).toContain('empty');
  });
});

// ===========================================================================
// parseXlsx
// ===========================================================================
describe('parseXlsx', () => {
  it('extracts URLs from first column', async () => {
    const file = createXlsxFile([
      ['https://example.com/page1'],
      ['https://example.com/page2'],
      ['https://example.com/page3'],
    ]);
    const result = await parseXlsx(file);

    expect(result.errors).toHaveLength(0);
    expect(result.urls).toEqual([
      'https://example.com/page1',
      'https://example.com/page2',
      'https://example.com/page3',
    ]);
  });

  it('extracts relative paths from first column', async () => {
    const file = createXlsxFile([
      ['/about'],
      ['/contact'],
      ['/products/item-1'],
    ]);
    const result = await parseXlsx(file);

    expect(result.errors).toHaveLength(0);
    expect(result.urls).toEqual(['/about', '/contact', '/products/item-1']);
  });

  it('ignores columns beyond the first', async () => {
    const file = createXlsxFile([
      ['https://example.com/page1', 'Title 1', 'Description 1'],
      ['https://example.com/page2', 'Title 2', 'Description 2'],
    ]);
    const result = await parseXlsx(file);

    expect(result.urls).toEqual([
      'https://example.com/page1',
      'https://example.com/page2',
    ]);
  });

  it('auto-skips common header rows', async () => {
    const headerNames = ['URL', 'url', 'Urls', 'Link', 'Page', 'Path', 'Address', 'Location', 'Source', 'Destination'];

    for (const header of headerNames) {
      const file = createXlsxFile([
        [header],
        ['https://example.com/page1'],
        ['https://example.com/page2'],
      ]);
      const result = await parseXlsx(file);

      expect(result.urls).toEqual([
        'https://example.com/page1',
        'https://example.com/page2',
      ]);
    }
  });

  it('does not skip a header when first cell is a URL', async () => {
    const file = createXlsxFile([
      ['https://example.com/'],
      ['https://example.com/page1'],
    ]);
    const result = await parseXlsx(file);

    expect(result.urls).toHaveLength(2);
    expect(result.urls[0]).toBe('https://example.com/');
  });

  it('warns when multiple sheets exist', async () => {
    const file = createXlsxFile(
      [['https://example.com/page1'], ['https://example.com/page2']],
      'multi.xlsx',
      ['URLs', 'Metadata', 'Notes']
    );
    const result = await parseXlsx(file);

    expect(result.warnings).toHaveLength(1);
    expect(result.warnings[0]).toContain('3 sheets');
    expect(result.warnings[0]).toContain('URLs');
    expect(result.urls).toHaveLength(2);
  });

  it('errors on empty sheet', async () => {
    const file = createXlsxFile([]);
    const result = await parseXlsx(file);

    expect(result.errors).toHaveLength(1);
    expect(result.errors[0]).toContain('empty');
  });

  it('errors when first column has no data', async () => {
    const file = createXlsxFile([
      [''],
      [''],
      [''],
    ]);
    const result = await parseXlsx(file);

    expect(result.errors).toHaveLength(1);
    expect(result.errors[0]).toContain('No data found');
  });

  it('errors when content does not look like URLs', async () => {
    const file = createXlsxFile([
      ['Apple'],
      ['Banana'],
      ['Cherry'],
      ['Date'],
      ['Elderberry'],
    ]);
    const result = await parseXlsx(file);

    expect(result.errors).toHaveLength(1);
    expect(result.errors[0]).toContain('does not appear to contain URLs');
  });

  it('warns when some rows are not URLs', async () => {
    const file = createXlsxFile([
      ['https://example.com/page1'],
      ['not-a-url'],
      ['https://example.com/page3'],
      ['also not a url'],
      ['https://example.com/page5'],
    ]);
    const result = await parseXlsx(file);

    expect(result.errors).toHaveLength(0);
    expect(result.warnings.length).toBeGreaterThan(0);
    expect(result.warnings[0]).toContain("don't appear to be valid URLs");
    // All values are still returned (including non-URLs)
    expect(result.urls).toHaveLength(5);
  });

  it('skips empty rows in the middle', async () => {
    const file = createXlsxFile([
      ['https://example.com/page1'],
      [''],
      ['https://example.com/page3'],
    ]);
    const result = await parseXlsx(file);

    expect(result.urls).toEqual([
      'https://example.com/page1',
      'https://example.com/page3',
    ]);
  });

  it('handles numeric cell values gracefully', async () => {
    const file = createXlsxFile([
      [12345],
      [67890],
    ]);
    const result = await parseXlsx(file);

    // Numbers don't look like URLs, so should error
    expect(result.errors).toHaveLength(1);
    expect(result.errors[0]).toContain('does not appear to contain URLs');
  });
});

// ===========================================================================
// urlsToTxtFile
// ===========================================================================
describe('urlsToTxtFile', () => {
  it('creates a File with correct content', async () => {
    const urls = ['https://example.com/', 'https://example.com/about'];
    const file = urlsToTxtFile(urls, 'sitemap.xml');
    const text = await file.text();

    expect(text).toBe('https://example.com/\nhttps://example.com/about');
  });

  it('replaces the original extension with .txt', () => {
    expect(urlsToTxtFile(['https://a.com'], 'sitemap.xml').name).toBe('sitemap.txt');
    expect(urlsToTxtFile(['https://a.com'], 'urls.xlsx').name).toBe('urls.txt');
    expect(urlsToTxtFile(['https://a.com'], 'data.csv').name).toBe('data.txt');
  });

  it('handles filenames without extension', () => {
    expect(urlsToTxtFile(['https://a.com'], 'noextension').name).toBe('noextension.txt');
  });

  it('creates a file with text/plain type', () => {
    const file = urlsToTxtFile(['https://a.com'], 'test.xml');
    expect(file.type).toBe('text/plain');
  });

  it('handles a large list of URLs', async () => {
    const urls = Array.from({ length: 1000 }, (_, i) => `https://example.com/page-${i}`);
    const file = urlsToTxtFile(urls, 'large.xml');
    const text = await file.text();
    const lines = text.split('\n');

    expect(lines).toHaveLength(1000);
    expect(lines[0]).toBe('https://example.com/page-0');
    expect(lines[999]).toBe('https://example.com/page-999');
  });

  it('handles single URL', async () => {
    const file = urlsToTxtFile(['https://example.com/only'], 'single.xml');
    const text = await file.text();

    expect(text).toBe('https://example.com/only');
  });
});


describe('sitemap page locations', () => {
  it.each(['', ' xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'])('excludes nested extension locations (%s)', namespace => {
    const result = parseXmlSitemap(`<urlset${namespace}>
      <url><loc>https://Example.com/Case%2FPath?q=A&amp;b=1</loc>
        <image xmlns="http://www.google.com/schemas/sitemap-image/1.1"><loc>https://example.com/photo.jpg</loc></image>
        <video><loc>https://example.com/video.mp4</loc></video>
      </url></urlset>`);
    expect(result.errors).toEqual([]);
    expect(result.urls).toEqual(['https://Example.com/Case%2FPath?q=A&b=1']);
  });
  it('accepts namespace-prefixed page records', () => {
    const result = parseXmlSitemap('<s:urlset xmlns:s="http://www.sitemaps.org/schemas/sitemap/0.9"><s:url><s:loc>https://example.com/Page</s:loc></s:url></s:urlset>');
    expect(result.urls).toEqual(['https://example.com/Page']);
  });
  it('does not treat extension-only locations as pages', () => {
    const result = parseXmlSitemap('<urlset><url><image><loc>https://example.com/photo.jpg</loc></image></url></urlset>');
    expect(result.urls).toEqual([]);
    expect(result.errors).toHaveLength(1);
  });
  it('rejects an unrelated namespace with sitemap-shaped tags', () => {
    expect(parseXmlSitemap('<urlset xmlns="urn:unrelated"><url><loc>https://example.com/a</loc></url></urlset>').errors).toHaveLength(1);
  });
});


describe('Excel first-used-column compatibility', () => {
  async function parseSheet(sheet: XLSX.WorkSheet) {
    const workbook = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(workbook, sheet, 'First');
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.aoa_to_sheet([['/ignored']]), 'Other');
    return parseXlsx(new File([XLSX.write(workbook, { type: 'array', bookType: 'xlsx' })], 'used-range.xlsx'));
  }

  it('preserves the first used column and raw URL spelling when the range starts at B3', async () => {
    const result = await parseSheet({
      '!ref': 'B3:CB8', B3: { t: 's', v: 'URLs' },
      B4: { t: 's', v: ' /Case/%2f?x=2&x=1#Section ' },
      B7: { t: 's', v: '/duplicate?' }, B8: { t: 's', v: '/duplicate?' },
      CB8: { t: 's', v: '/ignored-column' },
    });
    expect(result).toEqual({
      urls: ['/Case/%2f?x=2&x=1#Section', '/duplicate?', '/duplicate?'], errors: [],
      warnings: ['Excel file has 2 sheets. Only the first sheet ("First") will be used.'],
    });
  });

  it('keeps cached formula values and raw values rather than formatted display or hyperlink targets', async () => {
    const result = await parseSheet({
      '!ref': 'A1:C9', A1: { t: 's', v: 'URL' },
      A2: { t: 's', v: '/cached', f: '"/cached"' },
      A3: { t: 'n', v: 42, z: '"/formatted"' },
      A4: { t: 'b', v: true }, A5: { t: 'e', v: 7 },
      A6: { t: 'n', f: '1+1' },
      A7: { t: 's', v: '/label', l: { Target: 'https://ignored.example' } },
      C9: { t: 's', v: '/ignored-column' },
    });
    expect(result.urls).toEqual(['/cached', '42', 'true', '/label']);
    expect(result.errors).toEqual([]);
    expect(result.warnings).toHaveLength(2);
  });
});

// Sniffed ODS content remains compatible even when uploaded with an XLSX name.
it('preserves raw date serial values instead of timezone-specific Date text', async () => {
  const book = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(book, {
    '!ref': 'A1:A3', A1: { t: 's', v: 'URL' },
    A2: { t: 'n', v: 43832, z: 'yyyy-mm-dd' }, A3: { t: 's', v: '/kept' },
  }, 'First');
  const buffer = XLSX.write(book, { type: 'array', bookType: 'ods' });
  const result = await parseXlsx(new File([buffer], 'date-styled.xlsx'));
  expect(result.urls).toEqual(['43832', '/kept']);
  expect(result.warnings).toEqual(["1 of the first 2 rows don't appear to be valid URLs."]);
});

it('omits ODS missing formula values and error cells instead of proposing error text as a URL', async () => {
  const book = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(book, {
    '!ref': 'A1:A6', A1: { t: 's', v: 'URL' }, A2: { t: 'n', f: '1+1' },
    A3: { t: 'e', v: 7 }, A4: { t: 'e', v: 0 }, A5: { t: 's', v: 'Not URL' },
    A6: { t: 's', v: '/kept' },
  }, 'First');
  const buffer = XLSX.write(book, { type: 'array', bookType: 'ods' });
  const result = await parseXlsx(new File([buffer], 'errors.xlsx'));
  expect(result).toEqual({ urls: ['Not URL', '/kept'], warnings: ["1 of the first 2 rows don't appear to be valid URLs."], errors: [] });
});

/** Edit real OOXML metadata without allocating a million writer rows. */
function sparseWorkbook(sheet: XLSX.WorkSheet, range: string, relocateLastRow = false): File {
  const book = XLSX.utils.book_new(); XLSX.utils.book_append_sheet(book, sheet, 'URLs');
  const zip = XLSX.CFB.read(new Uint8Array(XLSX.write(book, { type: 'array', bookType: 'xlsx' })), { type: 'array' });
  const path = (zip.FullPaths as string[]).find(value => value.endsWith('/xl/worksheets/sheet1.xml'))!;
  const entry = XLSX.CFB.find(zip, path) as { content: Uint8Array };
  let xml = new TextDecoder().decode(entry.content).replace(/<dimension ref="[^"]+"/, `<dimension ref="${range}"`);
  if (relocateLastRow) xml = xml.replace(/r="C4"/g, 'r="C1048576"').replace(/<row r="4"/g, '<row r="1048576"');
  XLSX.CFB.utils.cfb_add(zip, path, new TextEncoder().encode(xml));
  const bytes = XLSX.CFB.write(zip, { type: 'array', fileType: 'zip', compression: true }) as Uint8Array;
  return new File([new Uint8Array(bytes)], 'sparse.xlsx');
}

describe('sparse declared workbook dimensions', () => {
  const exact = 'https://Example.com/Case%2FPath?q=A%20B';
  it.each([false, true])('preserves a non-A first column with a million-row extent (physical last row: %s)', async relocate => {
    const sheet = XLSX.utils.sheet_add_aoa({}, [['url', 'ignored'], [exact, '/not-imported']], { origin: 'C3' });
    const file = sparseWorkbook(sheet, 'C3:D1048576', relocate);
    expect(file.size).toBeLessThan(10_000);
    expect(await parseXlsx(file)).toEqual({ urls: [exact], warnings: [], errors: [] });
  });
  it('keeps a leading blank from promoting a later header and preserves error/formula/date values', async () => {
    const sheet: XLSX.WorkSheet = {
      C4: { t: 's', v: 'url' }, C5: { t: 's', v: exact },
      C6: { t: 'e', v: 36 }, C7: { t: 'n', v: 45292, z: 'yyyy-mm-dd' },
      C8: { t: 's', f: 'CONCAT("https://example.com/", "cached")', v: '/cached' },
      '!ref': 'C3:C8',
    };
    expect(await parseXlsx(sparseWorkbook(sheet, 'C3:C1048576'))).toEqual({
      urls: ['url', exact, '45292', '/cached'], warnings: ["2 of the first 4 rows don't appear to be valid URLs."], errors: [],
    });
  });
  it.each([2000, 20000, 1048576])('preserves a headerless first URL across a %i-row extent', async extent => {
    const sheet = XLSX.utils.sheet_add_aoa({}, [[exact], [], ['/last']], { origin: 'B5' });
    expect(await parseXlsx(sparseWorkbook(sheet, `B5:B${extent}`))).toEqual({ urls: [exact, '/last'], warnings: [], errors: [] });
  });
  it('preserves the library recovery for inverted metadata without walking its invalid extent', async () => {
    const sheet = XLSX.utils.aoa_to_sheet([[exact]]);
    expect(await parseXlsx(sparseWorkbook(sheet, 'A1048576:A1'))).toEqual({ urls: [exact], warnings: [], errors: [] });
  });
  it('does not read bytes after cancellation during an asynchronous dependency load', async () => {
    const controller = new AbortController();
    let reads = 0;
    const file = new File(['/url'], 'cancel.xlsx');
    file.arrayBuffer = async () => { reads += 1; return new ArrayBuffer(0); };
    const pending = parseXlsx(file, controller.signal); controller.abort();
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' }); expect(reads).toBe(0);
  });
});

it('stops before workbook parsing when cancellation happens during an uncancellable arrayBuffer read', async () => {
  const controller = new AbortController();
  let finish!: (value: ArrayBuffer) => void;
  let started!: () => void;
  const reading = new Promise<void>(resolve => { started = resolve; });
  const file = new File(['/url'], 'pending.xlsx');
  file.arrayBuffer = () => { started(); return new Promise(resolve => { finish = resolve; }); };
  const pending = parseXlsx(file, controller.signal); await reading;
  controller.abort(); finish(new ArrayBuffer(0));
  // An empty workbook would throw a parser error if the expensive read started.
  await expect(pending).rejects.toMatchObject({ name: 'AbortError' });
});
