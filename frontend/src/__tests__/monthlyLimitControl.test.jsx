/**
 * "Max/month" control on an existing leave type (Manage Leave Types).
 * Sends only the monthly limit; empty means "no limit"; shows the result.
 */
import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import MonthlyLimitControl from "../components/leave/MonthlyLimitControl";

const C = { border: "#ddd", surface: "#fff", text: "#111", sub: "#666" };
const COMP = "Compensatory Leave";

const setup = (props = {}) => {
  const save = props.save || vi.fn(async (lt, v) => ({ leave_type: lt, monthly_limit: v }));
  const onSaved = vi.fn();
  render(<MonthlyLimitControl leaveType={COMP} value={props.value ?? null} save={save} onSaved={onSaved} C={C} />);
  return { save, onSaved, input: screen.getByLabelText(/Compensatory Leave/), button: screen.getByText("Save") };
};

describe("MonthlyLimitControl", () => {
  it("saves 2 days per month for the type", async () => {
    const { save, onSaved, input, button } = setup();
    fireEvent.change(input, { target: { value: "2" } });
    fireEvent.click(button);
    await waitFor(() => expect(save).toHaveBeenCalledWith(COMP, 2));
    expect(onSaved).toHaveBeenCalled();
    expect(await screen.findByText(/Saved/)).toBeTruthy();
  });

  it("can be raised from 2 to 3", async () => {
    const { save, input, button } = setup({ value: 2 });
    expect(input.value).toBe("2");
    fireEvent.change(input, { target: { value: "3" } });
    fireEvent.click(button);
    await waitFor(() => expect(save).toHaveBeenCalledWith(COMP, 3));
  });

  it("clearing the box removes the limit", async () => {
    const { save, input, button } = setup({ value: 2 });
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.click(button);
    await waitFor(() => expect(save).toHaveBeenCalledWith(COMP, null));
  });

  it("does not save an out-of-range value or an unchanged one", () => {
    const { save, input, button } = setup({ value: 2 });
    expect(button.disabled).toBe(true);              // unchanged
    fireEvent.change(input, { target: { value: "40" } });
    expect(button.disabled).toBe(true);              // > 31
    fireEvent.click(button);
    expect(save).not.toHaveBeenCalled();
  });

  it("shows the server's error when the save is refused", async () => {
    const save = vi.fn(async () => ({ detail: "Only admin or HR can manage leave types" }));
    const { input, button } = setup({ save });
    fireEvent.change(input, { target: { value: "2" } });
    fireEvent.click(button);
    expect(await screen.findByText("Only admin or HR can manage leave types")).toBeTruthy();
  });
});
