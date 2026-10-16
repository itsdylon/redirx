import { useState, useRef, useId } from 'react';
import { Upload, File, X, AlertCircle } from 'lucide-react';
import { Button } from './ui/button';
import { Alert, AlertDescription } from './ui/alert';

interface FileUploadZoneProps {
  label: string;
  onFileUpload: (file: File) => void;
  onFileRemove: () => void;
  file: { name: string; rowCount: number } | null;
  validationError?: string | null;
  checkingFileName?: string | null;
}

export function FileUploadZone({ label, onFileUpload, onFileRemove, file, validationError, checkingFileName }: FileUploadZoneProps) {
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const inputId = useId();

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  };

  const handleDragLeave = () => {
    setIsDragging(false);
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);

    const droppedFile = e.dataTransfer.files[0];
    // The same validator handles picker and drop errors; unsupported drops
    // must not silently disappear.
    if (droppedFile) {
      onFileUpload(droppedFile);
    }
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selectedFile = e.target.files?.[0];
    if (selectedFile) {
      onFileUpload(selectedFile);
      e.target.value = '';
    }
  };

  const handleClick = () => {
    fileInputRef.current?.click();
  };

  const handleRemove = (e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    // Reset the native input so selecting the same file again triggers onChange.
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
    onFileRemove();
  };

  return (
    <div>
      <label htmlFor={inputId} className="block mb-2 text-foreground">{label}</label>

      {validationError && (
        <Alert variant="destructive" className="mb-3">
          <AlertCircle className="h-4 w-4" />
          <AlertDescription>{validationError}</AlertDescription>
        </Alert>
      )}

      <div
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        onClick={handleClick}
        className={`
          border-2 border-dashed p-8 text-center cursor-pointer
          transition-colors min-h-[200px] flex flex-col items-center justify-center
          ${isDragging ? 'border-primary bg-primary/10' : 'border-border bg-card'}
          ${file ? 'bg-muted' : ''}
          ${validationError ? 'border-destructive' : ''}
        `}
      >
        <input
          id={inputId}
          ref={fileInputRef}
          type="file"
          accept=".csv,.txt,.xml,.xlsx"
          onChange={handleFileSelect}
          className="hidden"
        />

        {checkingFileName ? (
          <div className="w-full space-y-4">
            <p role="status" className="break-all">Checking {checkingFileName}…</p>
            <p className="text-sm text-muted-foreground">Reading URLs and checking the file. Nothing has been uploaded yet.</p>
            <div className="flex flex-wrap justify-center gap-3">
              <Button variant="outline" type="button">Choose another file</Button>
              <Button variant="outline" type="button" onClick={handleRemove} aria-label={`Cancel checking ${checkingFileName}`}>Cancel check</Button>
            </div>
          </div>
        ) : !file ? (
          <>
            <Upload className="h-12 w-12 text-muted-foreground mb-4" />
            <p className="text-foreground mb-1">
              Drag and drop file here
            </p>
            <p className="text-muted-foreground text-sm mb-4">or</p>
            <Button variant="outline" type="button">
              Browse Files
            </Button>
            <p className="text-muted-foreground text-xs mt-4">
              Accepted formats: .csv, .txt, .xml (sitemap), .xlsx
            </p>
          </>
        ) : (
          <div className="w-full">
            <div className="flex items-center justify-between p-4 border border-border bg-card">
              <div className="flex min-w-0 items-center gap-3">
                <File className="h-8 w-8 shrink-0 text-muted-foreground" />
                <div className="min-w-0 text-left">
                  <p className="text-foreground break-all">{file.name}</p>
                  <p className="text-muted-foreground text-sm">{file.rowCount} rows</p>
                </div>
              </div>
              <Button
                variant="ghost"
                size="icon"
                onClick={handleRemove}
                type="button"
                aria-label={`Remove ${file.name}`}
                className="shrink-0"
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
            <Button variant="outline" type="button" className="mt-4">
              Replace file
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}
