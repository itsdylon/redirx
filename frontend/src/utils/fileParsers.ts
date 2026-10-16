export interface ParseResult {
  urls: string[];
  warnings: string[];
  errors: string[];
}

/**
 * Checks if a string looks like a URL (absolute or relative path)
 */
export function looksLikeUrl(value: string): boolean {
  if (!value) return false;
  return /^https?:\/\//i.test(value) || value.startsWith('/');
}

/**
 * Parses an XML sitemap and extracts URLs from <loc> elements.
 */
export function parseXmlSitemap(xmlText: string): ParseResult {
  const result: ParseResult = { urls: [], warnings: [], errors: [] };

  const parser = new DOMParser();
  const doc = parser.parseFromString(xmlText, 'application/xml');

  // Check for XML parse errors
  const parseError = doc.querySelector('parsererror');
  if (parseError) {
    result.errors.push('File is not valid XML.');
    return result;
  }

  const root = doc.documentElement;

  // Detect sitemap index files
  if (root.localName === 'sitemapindex') {
    result.errors.push(
      'This is a sitemap index file, not a sitemap. Please upload an individual sitemap (e.g., sitemap-posts.xml) instead.'
    );
    return result;
  }

  // Must be a <urlset> root
  if (root.localName !== 'urlset') {
    result.errors.push(
      'File is not a sitemap. Expected a <urlset> root element but found <' + root.localName + '>.'
    );
    return result;
  }

  // Only direct sitemap url/loc children describe pages. Descendant locs may
  // belong to image/video extensions (including a different default namespace).
  const NS = 'http://www.sitemaps.org/schemas/sitemap/0.9';
  if (root.namespaceURI && root.namespaceURI !== NS) {
    result.errors.push('File is not a sitemap. The <urlset> namespace is not supported.');
    return result;
  }
  const sameNamespace = (element: Element) => element.namespaceURI === root.namespaceURI;
  const locs = Array.from(root.children)
    .filter(element => element.localName === 'url' && sameNamespace(element))
    .flatMap(element => Array.from(element.children)
      .filter(child => child.localName === 'loc' && sameNamespace(child)));

  if (locs.length === 0) {
    result.errors.push('Sitemap contains no <loc> elements (no URLs found).');
    return result;
  }

  for (let i = 0; i < locs.length; i++) {
    const url = locs[i].textContent?.trim();
    if (url) {
      result.urls.push(url);
    }
  }

  if (result.urls.length === 0) {
    result.errors.push('All <loc> elements in the sitemap are empty.');
    return result;
  }

  return result;
}

/**
 * Parses an XLSX file and extracts URLs from the first column of the first sheet.
 */
export async function parseXlsx(file: File, signal?: AbortSignal): Promise<ParseResult> {
  const result: ParseResult = { urls: [], warnings: [], errors: [] };

  // Load the spreadsheet engine only for an actual Excel import.
  signal?.throwIfAborted();
  const XLSX = await import('xlsx');
  signal?.throwIfAborted();
  const buffer = await file.arrayBuffer();
  signal?.throwIfAborted();
  const workbook = XLSX.read(buffer, { type: 'array', sheets: 0 });

  if (workbook.SheetNames.length === 0) {
    result.errors.push('Excel file contains no sheets.');
    return result;
  }

  if (workbook.SheetNames.length > 1) {
    result.warnings.push(
      `Excel file has ${workbook.SheetNames.length} sheets. Only the first sheet ("${workbook.SheetNames[0]}") will be used.`
    );
  }

  const sheet = workbook.Sheets[workbook.SheetNames[0]];
  // Preserve the declared first used column and initial row, including an
  // initial blank/error cell (which must not turn a later value into a header).
  // Project populated cells before conversion: a tiny workbook can declare a
  // million blank rows. Walking that rectangle blocks the browser unnecessarily.
  const reference = sheet?.['!ref'];
  let projected = sheet;
  if (reference) {
    const range = XLSX.utils.decode_range(reference);
    const coordinates = [range.s.r, range.s.c, range.e.r, range.e.c];
    if (!/^[A-Z]+[1-9]\d*(?::[A-Z]+[1-9]\d*)?$/.test(reference)
      || coordinates.some(value => !Number.isSafeInteger(value) || value < 0)
      || range.e.r < range.s.r || range.e.c < range.s.c) {
      result.errors.push('Excel sheet has an invalid cell range. Save it again as an .xlsx file and retry.');
      return result;
    }
    const column = XLSX.utils.encode_col(range.s.c);
    // Ordinary extents retain the cheaper rectangular converter. Use the
    // populated-cell projection when the declared row walk would exceed even
    // the encoded byte count. This is only a cost heuristic, never an input cap;
    // both branches preserve the same values, header position and warnings.
    if (range.e.r - range.s.r + 1 <= file.size) {
      range.e.c = range.s.c;
      for (let row = range.s.r; row <= range.e.r; row++) {
        const cell = sheet[`${column}${row + 1}`];
        if (cell?.t === 'n') delete cell.z;
      }
      // Keep the original first-column range without copying unrelated cells.
      projected = sheet;
      const rows: string[][] = XLSX.utils.sheet_to_json(projected, { header: 1, defval: '', range });
      return extractColumn(rows, result);
    }
    const selected = Object.keys(sheet).flatMap(address => {
      if (!address.startsWith(column)) return [];
      const suffix = address.slice(column.length);
      if (!/^[1-9]\d*$/.test(suffix)) return [];
      const row = Number(suffix) - 1;
      return row >= range.s.r && row <= range.e.r ? [{ row, cell: sheet[address] }] : [];
    }).sort((a, b) => a.row - b.row);
    // Keep one blank initial row when the first physical value starts later.
    projected = { A1: { t: 's', v: '' } };
    let nextRow = 0;
    for (const { row, cell } of selected) {
      if (nextRow === 0 && row > range.s.r) nextRow = 1;
      // 0.20's converter honors numeric date formatting even with raw output.
      // Preserve historical raw serials without modifying the source cell.
      projected[`A${++nextRow}`] = cell?.t === 'n' ? { ...cell, z: undefined } : cell;
    }
    projected['!ref'] = `A1:A${Math.max(1, nextRow)}`;
  }
  const rows: string[][] = XLSX.utils.sheet_to_json(projected, { header: 1, defval: '' });
  return extractColumn(rows, result);
}

function extractColumn(rows: string[][], result: ParseResult): ParseResult {
  if (rows.length === 0) {
    result.errors.push('Sheet is empty.');
    return result;
  }

  // Check if first row is a header
  const HEADER_NAMES = /^(url|address|link|page|path|location|source|destination)s?$/i;
  let startRow = 0;
  const firstCell = String(rows[0][0] ?? '').trim();
  if (HEADER_NAMES.test(firstCell)) {
    startRow = 1;
  }

  // Extract first column values
  const values: string[] = [];
  for (let i = startRow; i < rows.length; i++) {
    const cell = String(rows[i][0] ?? '').trim();
    if (cell) {
      values.push(cell);
    }
  }

  if (values.length === 0) {
    result.errors.push('No data found in the first column of the sheet.');
    return result;
  }

  // Sample-check for URLs
  const sampleSize = Math.min(20, values.length);
  let nonUrlCount = 0;
  for (let i = 0; i < sampleSize; i++) {
    if (!looksLikeUrl(values[i])) {
      nonUrlCount++;
    }
  }

  if (nonUrlCount === sampleSize) {
    result.errors.push(
      'First column does not appear to contain URLs. Each row should contain a URL (e.g., https://example.com/page or /page).'
    );
    return result;
  }

  if (nonUrlCount > 0) {
    result.warnings.push(
      `${nonUrlCount} of the first ${sampleSize} rows don't appear to be valid URLs.`
    );
  }

  result.urls = values;
  return result;
}

/**
 * Converts a list of URLs into a .txt File object (one URL per line).
 */
export function urlsToTxtFile(urls: string[], originalFileName: string): File {
  const content = urls.join('\n');
  const blob = new Blob([content], { type: 'text/plain' });
  const baseName = originalFileName.replace(/\.[^.]+$/, '');
  return new File([blob], baseName + '.txt', { type: 'text/plain' });
}
