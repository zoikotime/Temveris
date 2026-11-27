/**
 * Supporting-document upload on Apply Leave.
 *
 * The regression these guard: the upload field used to be rendered behind
 * `form.leave_type === "Medical Leave"`, a type name no leave catalogue
 * contains. Sick Leave therefore advertised "📎 Document Required" in its info
 * card and then gave the employee nowhere to attach one. Visibility is now
 * driven by the type's own `doc_required` flag, and the blocking behaviour by
 * `evidence_gate` — the same split the backend makes.
 */
import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import DocumentUpload, { validateDocument, formatBytes } from "../components/leave/DocumentUpload";

const C = {
  text: "#101828", textSub: "#475467", textLight: "#98A2B3",
  border: "#E4E7EC", surface: "#fff", surfaceAlt: "#F8FAFC",
  brand: "#11496A", brandBg: "#E7F0F6",
  error: "#B91C1C", errorBg: "#FEF2F2", errorBorder: "#FCA5A5",
  successBg: "#E8F7EE", successBorder: "#A8E0BD",
};

const CONTRACT = { accept: ".pdf,.jpg,.jpeg,.png", formats: ["PDF", "JPG", "JPEG", "PNG"], maxMb: 10 };

const mkFile = (name, size, type = "application/pdf") => {
  const f = new File(["x"], name, { type });
  Object.defineProperty(f, "size", { value: size });
  return f;
};

describe("validateDocument", () => {
  it("accepts a PDF inside the limit", () => {
    expect(validateDocument(mkFile("cert.pdf", 2 * 1024 * 1024), CONTRACT)).toBeNull();
  });

  it("rejects a file type outside the published list", () => {
    const msg = validateDocument(mkFile("payload.exe", 1024), CONTRACT);
    expect(msg).toMatch(/not an accepted file type/i);
    expect(msg).toMatch(/PDF, JPG, JPEG, PNG/);
  });

  it("rejects a file over the published size cap", () => {
    const msg = validateDocument(mkFile("scan.pdf", 11 * 1024 * 1024), CONTRACT);
    expect(msg).toMatch(/exceeds the 10 MB limit/i);
  });

  it("rejects an empty file — it uploads fine and shows the reviewer nothing", () => {
    expect(validateDocument(mkFile("blank.pdf", 0), CONTRACT)).toMatch(/is empty/i);
  });

  it("honours the limit the SERVER published, not a hardcoded one", () => {
    // A 6 MB file passes under a 10 MB cap and fails under a 5 MB cap.
    const six = mkFile("scan.pdf", 6 * 1024 * 1024);
    expect(validateDocument(six, { ...CONTRACT, maxMb: 10 })).toBeNull();
    expect(validateDocument(six, { ...CONTRACT, maxMb: 5 })).toMatch(/exceeds the 5 MB limit/i);
  });
});

describe("formatBytes", () => {
  it("renders KB and MB", () => {
    expect(formatBytes(2048)).toBe("2 KB");
    expect(formatBytes(3.5 * 1024 * 1024)).toBe("3.5 MB");
  });
});

describe("DocumentUpload", () => {
  it("shows the dropzone, the accepted formats and the size cap", () => {
    render(<DocumentUpload C={C} onChange={() => {}} {...CONTRACT} />);
    expect(screen.getByText(/Upload Document/i)).toBeInTheDocument();
    expect(screen.getByText(/PDF, JPG, JPEG, PNG · Max 10 MB/i)).toBeInTheDocument();
  });

  it("marks the field required with an asterisk when the type gates on it", () => {
    const { container } = render(
      <DocumentUpload C={C} onChange={() => {}} required {...CONTRACT} />,
    );
    expect(container.querySelector("label").textContent).toMatch(/Supporting Document\s*\*/);
  });

  it("stays silent about the missing document until Submit is pressed", () => {
    const { rerender } = render(
      <DocumentUpload C={C} onChange={() => {}} required {...CONTRACT} />,
    );
    expect(screen.queryByText(/Supporting document is required/i)).toBeNull();

    rerender(<DocumentUpload C={C} onChange={() => {}} required showRequiredError {...CONTRACT} />);
    expect(
      screen.getByText("Supporting document is required for this leave type."),
    ).toBeInTheDocument();
  });

  it("surfaces the file name, size and a Remove control once a file is picked", () => {
    const onChange = vi.fn();
    render(
      <DocumentUpload
        C={C}
        onChange={onChange}
        file={mkFile("medical-certificate.pdf", 1536 * 1024)}
        required
        {...CONTRACT}
      />,
    );
    expect(screen.getByText("medical-certificate.pdf")).toBeInTheDocument();
    expect(screen.getByText(/1\.5 MB/)).toBeInTheDocument();

    fireEvent.click(screen.getByText(/Remove/i));
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("rejects an invalid drop and does not hand the file up", () => {
    const onChange = vi.fn();
    const { container } = render(
      <DocumentUpload C={C} onChange={onChange} {...CONTRACT} />,
    );
    const zone = container.querySelector('[role="button"]');
    fireEvent.drop(zone, { dataTransfer: { files: [mkFile("virus.exe", 2048)] } });

    expect(screen.getByText(/not an accepted file type/i)).toBeInTheDocument();
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("accepts a valid drop", () => {
    const onChange = vi.fn();
    const { container } = render(
      <DocumentUpload C={C} onChange={onChange} {...CONTRACT} />,
    );
    const good = mkFile("cert.pdf", 4096);
    fireEvent.drop(container.querySelector('[role="button"]'), {
      dataTransfer: { files: [good] },
    });
    expect(onChange).toHaveBeenCalledWith(good);
  });
});
