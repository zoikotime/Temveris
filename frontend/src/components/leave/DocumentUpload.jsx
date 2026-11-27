import React, { useCallback, useRef, useState } from "react";

// DocumentUpload — the "Supporting Document" dropzone on Apply Leave.
//
// Rendered whenever the SELECTED leave type carries `doc_required`, and marked
// mandatory only when that type also carries `evidence_gate` (the flag the
// backend actually blocks on — see LeavePage's `evidenceMissing`).
//
// The accepted formats and the size cap are NOT hardcoded here. They arrive on
// the leave type from GET /leave/types (`doc_accept`, `doc_formats`,
// `doc_max_mb`), which publishes the same constants /leave/apply enforces, so
// the dropzone can never advertise a limit the server would reject. The props
// carry defaults only so the component still renders during the first fetch.

const ICON_FOR = (name = "") =>
  /\.pdf$/i.test(name) ? "📕" : /\.(png|jpe?g|webp|gif)$/i.test(name) ? "🖼️" : "📄";

export function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Validate a picked file against the published contract.
 * Returns null when the file is acceptable, otherwise a display message.
 * Exported so the submit handler can re-check without duplicating the rules.
 */
export function validateDocument(file, { accept, formats, maxMb }) {
  if (!file) return null;
  const exts = String(accept || "")
    .split(",")
    .map((e) => e.trim().toLowerCase())
    .filter(Boolean);
  const name = (file.name || "").toLowerCase();
  if (exts.length && !exts.some((e) => name.endsWith(e))) {
    return `“${file.name}” is not an accepted file type. Please upload a ${(formats || []).join(", ")} file.`;
  }
  // 0-byte files upload "successfully" and give the reviewer nothing to read.
  if (file.size === 0) {
    return `“${file.name}” is empty. Please choose a different file.`;
  }
  if (maxMb && file.size > maxMb * 1024 * 1024) {
    return `“${file.name}” is ${formatBytes(file.size)}, which exceeds the ${maxMb} MB limit.`;
  }
  return null;
}

export default function DocumentUpload({
  C,
  T = {},
  file,
  onChange,
  required = false,
  // Published by GET /leave/types; defaults cover the pre-fetch render only.
  accept = ".pdf,.jpg,.jpeg,.png",
  formats = ["PDF", "JPG", "JPEG", "PNG"],
  maxMb = 10,
  // Set once the employee has tried to submit, so the "required" message
  // appears on a real attempt rather than the moment the field is revealed.
  showRequiredError = false,
  disabled = false,
  hint = "",
}) {
  const inputRef = useRef(null);
  const [dragging, setDragging] = useState(false);
  const [fileError, setFileError] = useState(null);

  const accepted = useCallback(
    (picked) => {
      const problem = validateDocument(picked, { accept, formats, maxMb });
      if (problem) {
        setFileError(problem);
        onChange(null);
        return;
      }
      setFileError(null);
      onChange(picked);
    },
    [accept, formats, maxMb, onChange],
  );

  const onDrop = (e) => {
    e.preventDefault();
    setDragging(false);
    if (disabled) return;
    const picked = e.dataTransfer?.files?.[0];
    if (picked) accepted(picked);
  };

  const openPicker = () => {
    if (disabled) return;
    inputRef.current?.click();
  };

  const remove = () => {
    setFileError(null);
    onChange(null);
    // Clear the native input too, so re-picking the SAME file still fires
    // onChange (the browser suppresses it when the value is unchanged).
    if (inputRef.current) inputRef.current.value = "";
  };

  const missing = required && !file;
  const showMissing = missing && showRequiredError;
  const borderColor = showMissing || fileError ? C.errorBorder : dragging ? C.brand : C.border;

  return (
    <div style={{ marginBottom: 20 }}>
      <label
        style={{
          display: "block",
          fontSize: 13,
          fontWeight: 600,
          color: C.text,
          marginBottom: 6,
        }}
      >
        {T.supportingDocument || "Supporting Document"}
        {required && <span style={{ color: C.error, marginLeft: 4 }}>*</span>}
      </label>

      {hint && (
        <p style={{ margin: "0 0 8px", fontSize: 12, color: C.textSub, lineHeight: 1.5 }}>{hint}</p>
      )}

      <input
        ref={inputRef}
        type="file"
        accept={accept}
        disabled={disabled}
        onChange={(e) => {
          const picked = e.target.files?.[0] || null;
          if (picked) accepted(picked);
        }}
        style={{ display: "none" }}
      />

      {!file ? (
        <div
          onClick={openPicker}
          onDragOver={(e) => {
            e.preventDefault();
            if (!disabled) setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          role="button"
          tabIndex={disabled ? -1 : 0}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              openPicker();
            }
          }}
          style={{
            border: `2px dashed ${borderColor}`,
            borderRadius: 10,
            background: dragging ? C.brandBg : showMissing || fileError ? C.errorBg : C.surfaceAlt,
            // Generous on desktop, compact on a phone — the dropzone is the
            // tallest block on the form and a fixed 32px would push Submit
            // below the fold on a small screen.
            padding: "clamp(18px, 5vw, 28px) 16px",
            textAlign: "center",
            cursor: disabled ? "not-allowed" : "pointer",
            opacity: disabled ? 0.6 : 1,
            transition: "border-color .15s, background .15s",
            boxSizing: "border-box",
          }}
        >
          <div style={{ fontSize: 26, lineHeight: 1, marginBottom: 8 }} aria-hidden="true">
            ⬆️
          </div>
          <p style={{ margin: "0 0 12px", fontSize: 13, color: C.textSub }}>
            {T.dropDocumentHere || "Drag and drop your file here, or"}
          </p>
          <span
            style={{
              display: "inline-block",
              background: C.brand,
              color: "#fff",
              borderRadius: 8,
              padding: "9px 20px",
              fontSize: 13,
              fontWeight: 700,
            }}
          >
            {T.uploadDocument || "Upload Document"}
          </span>
          <p style={{ margin: "12px 0 0", fontSize: 11.5, color: C.textLight }}>
            {(formats || []).join(", ")} · {T.maxSize || "Max"} {maxMb} MB
          </p>
        </div>
      ) : (
        <div
          style={{
            border: `1px solid ${C.successBorder}`,
            borderRadius: 10,
            background: C.successBg,
            padding: "12px 14px",
            display: "flex",
            alignItems: "center",
            gap: 12,
            // Wraps to two rows on a narrow screen instead of squashing the
            // filename into an unreadable column.
            flexWrap: "wrap",
            boxSizing: "border-box",
          }}
        >
          <div style={{ fontSize: 22, lineHeight: 1 }} aria-hidden="true">
            {ICON_FOR(file.name)}
          </div>
          <div style={{ flex: 1, minWidth: 140 }}>
            <div
              style={{
                fontSize: 13,
                fontWeight: 600,
                color: C.text,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
              title={file.name}
            >
              {file.name}
            </div>
            <div style={{ fontSize: 11.5, color: C.textSub, marginTop: 2 }}>
              {formatBytes(file.size)} · {T.readyToSubmit || "Ready to submit"}
            </div>
          </div>
          <button
            type="button"
            onClick={remove}
            disabled={disabled}
            style={{
              flexShrink: 0,
              background: "transparent",
              border: `1px solid ${C.border}`,
              color: C.error,
              borderRadius: 6,
              padding: "7px 14px",
              fontSize: 12.5,
              fontWeight: 600,
              cursor: disabled ? "not-allowed" : "pointer",
              fontFamily: "inherit",
            }}
          >
            {T.remove || "Remove"}
          </button>
        </div>
      )}

      {fileError && (
        <p style={{ margin: "8px 0 0", fontSize: 12, color: C.error, fontWeight: 600 }}>
          ❌ {fileError}
        </p>
      )}
      {showMissing && !fileError && (
        <p style={{ margin: "8px 0 0", fontSize: 12, color: C.error, fontWeight: 600 }}>
          {T.documentRequiredError || "Supporting document is required for this leave type."}
        </p>
      )}
    </div>
  );
}
