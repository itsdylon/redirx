import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { FileUploadZone } from './FileUploadZone';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function renderZone(overrides: Partial<Parameters<typeof FileUploadZone>[0]> = {}) {
  const defaults = {
    label: 'Old Site URLs',
    onFileUpload: vi.fn(),
    onFileRemove: vi.fn(),
    file: null,
    validationError: null,
  };
  const props = { ...defaults, ...overrides };
  return { ...render(<FileUploadZone {...props} />), props };
}

function createDragEvent(file: File): Partial<React.DragEvent> {
  return {
    preventDefault: vi.fn(),
    dataTransfer: { files: [file] } as unknown as DataTransfer,
  };
}

// ===========================================================================
// Rendering
// ===========================================================================
describe('FileUploadZone — rendering', () => {
  it('renders the label', () => {
    renderZone({ label: 'New Site URLs' });
    expect(screen.getByText('New Site URLs')).toBeInTheDocument();
  });

  it('shows accepted formats text', () => {
    renderZone();
    expect(screen.getByText(/Accepted formats/)).toBeInTheDocument();
    expect(screen.getByText(/\.csv.*\.txt.*\.xml.*\.xlsx/)).toBeInTheDocument();
  });

  it('shows drag-and-drop prompt when no file', () => {
    renderZone();
    expect(screen.getByText(/Drag and drop/)).toBeInTheDocument();
    expect(screen.getByText('Browse Files')).toBeInTheDocument();
  });

  it('shows file info when a file is provided', () => {
    renderZone({
      file: { name: 'urls.csv', rowCount: 42 },
    });
    expect(screen.getByText('urls.csv')).toBeInTheDocument();
    expect(screen.getByText('42 rows')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Replace file' })).toBeInTheDocument();
  });

  it('shows validation error when present', () => {
    renderZone({
      validationError: 'File must have a .csv or .txt extension',
    });
    expect(screen.getByText('File must have a .csv or .txt extension')).toBeInTheDocument();
  });

  it('does not show error when validationError is null', () => {
    renderZone({ validationError: null });
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

// ===========================================================================
// File input accept attribute
// ===========================================================================
describe('FileUploadZone — file input', () => {
  it('has correct accept attribute with all four formats', () => {
    renderZone();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).toBeTruthy();
    expect(input.accept).toBe('.csv,.txt,.xml,.xlsx');
  });

  it('calls onFileUpload when a file is selected via input', () => {
    const { props } = renderZone();
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;

    const testFile = new File(['test'], 'urls.csv', { type: 'text/csv' });
    fireEvent.change(input, { target: { files: [testFile] } });

    expect(props.onFileUpload).toHaveBeenCalledWith(testFile);
  });
});

// ===========================================================================
// Drag & drop
// ===========================================================================
describe('FileUploadZone — drag and drop', () => {
  it('accepts dropped .csv files', () => {
    const { props } = renderZone();
    const zone = screen.getByText(/Drag and drop/).closest('div[class]')!;

    const file = new File(['test'], 'urls.csv', { type: 'text/csv' });
    fireEvent.drop(zone, createDragEvent(file));

    expect(props.onFileUpload).toHaveBeenCalledWith(file);
  });

  it('accepts dropped .txt files', () => {
    const { props } = renderZone();
    const zone = screen.getByText(/Drag and drop/).closest('div[class]')!;

    const file = new File(['test'], 'urls.txt', { type: 'text/plain' });
    fireEvent.drop(zone, createDragEvent(file));

    expect(props.onFileUpload).toHaveBeenCalledWith(file);
  });

  it('accepts dropped .xml files', () => {
    const { props } = renderZone();
    const zone = screen.getByText(/Drag and drop/).closest('div[class]')!;

    const file = new File(['test'], 'sitemap.xml', { type: 'application/xml' });
    fireEvent.drop(zone, createDragEvent(file));

    expect(props.onFileUpload).toHaveBeenCalledWith(file);
  });

  it('accepts dropped .xlsx files', () => {
    const { props } = renderZone();
    const zone = screen.getByText(/Drag and drop/).closest('div[class]')!;

    const file = new File(['test'], 'urls.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    });
    fireEvent.drop(zone, createDragEvent(file));

    expect(props.onFileUpload).toHaveBeenCalledWith(file);
  });

  it('delegates validation for dropped files with unsupported extensions', () => {
    const { props } = renderZone();
    const zone = screen.getByText(/Drag and drop/).closest('div[class]')!;

    const file = new File(['test'], 'data.json', { type: 'application/json' });
    fireEvent.drop(zone, createDragEvent(file));

    expect(props.onFileUpload).toHaveBeenCalledWith(file);
  });

  it('delegates validation for dropped .pdf files', () => {
    const { props } = renderZone();
    const zone = screen.getByText(/Drag and drop/).closest('div[class]')!;

    const file = new File(['test'], 'report.pdf', { type: 'application/pdf' });
    fireEvent.drop(zone, createDragEvent(file));

    expect(props.onFileUpload).toHaveBeenCalledWith(file);
  });

  it('delegates validation for dropped .xls (legacy Excel) files', () => {
    const { props } = renderZone();
    const zone = screen.getByText(/Drag and drop/).closest('div[class]')!;

    const file = new File(['test'], 'old.xls', { type: 'application/vnd.ms-excel' });
    fireEvent.drop(zone, createDragEvent(file));

    expect(props.onFileUpload).toHaveBeenCalledWith(file);
  });
});

// ===========================================================================
// Visual states
// ===========================================================================
describe('FileUploadZone — visual states', () => {
  it('applies destructive border class when validation error is present', () => {
    const { container } = render(
      <FileUploadZone
        label="Test"
        onFileUpload={vi.fn()}
        onFileRemove={vi.fn()}
        file={null}
        validationError="Some error"
      />
    );
    const zone = container.querySelector('.border-destructive');
    expect(zone).toBeTruthy();
  });

  it('applies muted background when file is present', () => {
    const { container } = render(
      <FileUploadZone
        label="Test"
        onFileUpload={vi.fn()}
        onFileRemove={vi.fn()}
        file={{ name: 'urls.csv', rowCount: 5 }}
        validationError={null}
      />
    );
    const zone = container.querySelector('.bg-muted');
    expect(zone).toBeTruthy();
  });
});

describe('FileUploadZone — removal', () => {
  it('calls onFileRemove when the remove button is clicked', () => {
    const onFileRemove = vi.fn();
    const onFileUpload = vi.fn();
    renderZone({
      file: { name: 'urls.csv', rowCount: 5 },
      onFileRemove,
      onFileUpload,
    });

    const removeButton = screen.getByRole('button', { name: 'Remove urls.csv' });
    fireEvent.click(removeButton);

    expect(onFileRemove).toHaveBeenCalledTimes(1);
    expect(onFileUpload).not.toHaveBeenCalled();
  });

  it('does not trigger file input click when remove is clicked', () => {
    const clickSpy = vi.spyOn(HTMLInputElement.prototype, 'click');
    const onFileRemove = vi.fn();
    renderZone({
      file: { name: 'urls.csv', rowCount: 5 },
      onFileRemove,
    });

    const removeButton = screen.getByRole('button', { name: 'Remove urls.csv' });
    fireEvent.click(removeButton);

    expect(clickSpy).not.toHaveBeenCalled();
    clickSpy.mockRestore();
  });
});

it('announces a pending filename and lets the user cancel without opening the file picker', () => {
  const { props, container } = renderZone({ checkingFileName: 'pending.xlsx' });
  expect(screen.getByRole('status')).toHaveTextContent('Checking pending.xlsx');
  expect(screen.getByText(/Nothing has been uploaded yet/)).toBeInTheDocument();
  const input = container.querySelector('input')!; const open = vi.spyOn(input, 'click');
  fireEvent.click(screen.getByRole('button', { name: 'Cancel checking pending.xlsx' }));
  expect(props.onFileRemove).toHaveBeenCalledOnce(); expect(open).not.toHaveBeenCalled();
});
