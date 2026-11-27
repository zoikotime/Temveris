import { useState } from "react";
import { useLanguage } from "../context/LanguageContext";

function AddEmployee() {
  const { T } = useLanguage();
  const [newEmp, setNewEmp] = useState({
    name: "",
    email: "",
    password: "",
    role: "employee",
    designation: "",
    phone: "",
    employment_type: "full_time",
    // Official Start Date — first working day. Mandatory per the leave
    // eligibility spec; the backend uses it (plus the tenant waiting period)
    // to gate paid-leave applications.
    start_date: "",
  });
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState(null); // { text, type }

  const handleChange = (e) => {
    let value = e.target.value;
    // Phone: keep only digits, spaces, hyphens, parentheses and a leading "+".
    if (e.target.name === "phone") {
      value = value.replace(/[^\d+\-\s()]/g, "");
    }
    setNewEmp({
      ...newEmp,
      [e.target.name]: value
    });
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setMessage(null);

    if (!newEmp.name || !newEmp.email || !newEmp.password) {
      setMessage({ text: T.pleaseFillRequired || "Please fill required fields (Name, Email, Password)", type: "error" });
      return;
    }
    if (!newEmp.start_date) {
      setMessage({ text: T.startDateRequired || "Official Start Date is required — it drives leave eligibility.", type: "error" });
      return;
    }

    const token = localStorage.getItem("token");
    if (!token) {
      setMessage({ text: T.notLoggedIn || "You are not logged in", type: "error" });
      return;
    }

    setLoading(true);
    try {
      const res = await fetch("/employees", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${token}`
        },
        body: JSON.stringify(newEmp)
      });
      const data = await res.json();
      if (res.ok) {
        setMessage({ text: `✅ ${T.employeeAddedSuccess || "Employee added successfully"}`, type: "success" });
        setNewEmp({
          name: "",
          email: "",
          password: "",
          role: "employee",
          designation: "",
          phone: "",
          employment_type: "full_time",
          start_date: "",
        });
      } else {
        setMessage({ text: data.detail || (T.failedToAddEmployee || "Failed to add employee"), type: "error" });
      }
    } catch (error) {
      console.error(error);
      setMessage({ text: T.serverError || "Server error", type: "error" });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <h3>{T.addWorker || "Add Worker"}</h3>

      {message && (
        <div style={{
          padding: "10px 14px",
          marginBottom: 12,
          borderRadius: 6,
          fontSize: 13,
          fontWeight: 600,
          background: message.type === "error" ? "#FEF3F2" : "#E8F7EE",
          color: message.type === "error" ? "#B42318" : "#2F8F5C",
          border: `1px solid ${message.type === "error" ? "#FECDCA" : "#A8E0BD"}`
        }}>
          {message.text}
        </div>
      )}

      <form
        onSubmit={handleSubmit}
        style={{ display: "flex", flexDirection: "column", gap: "10px" }}
      >
        <input
          name="name"
          placeholder={T.fullName || "Full Name"}
          value={newEmp.name}
          onChange={handleChange}
          required
        />
        <input
          name="email"
          type="email"
          placeholder={T.email || "Email"}
          value={newEmp.email}
          onChange={handleChange}
          required
        />
        <input
          name="password"
          type="password"
          placeholder={T.password || "Password"}
          value={newEmp.password}
          onChange={handleChange}
          required
        />
        <select name="role" value={newEmp.role} onChange={handleChange}>
          <option value="employee">{T.employee || "Employee"}</option>
          <option value="manager">{T.manager || "Manager"}</option>
          <option value="admin">{T.adminLabel || "Admin"}</option>
        </select>
        <input
          name="designation"
          placeholder={T.designation || "Designation"}
          value={newEmp.designation}
          onChange={handleChange}
        />
        <input
          name="phone"
          type="tel"
          inputMode="tel"
          autoComplete="tel"
          pattern="[+0-9\-\s()]*"
          placeholder={T.phone || "Phone"}
          value={newEmp.phone}
          onChange={handleChange}
        />
        <select
          name="employment_type"
          value={newEmp.employment_type}
          onChange={handleChange}
        >
          <option value="full_time">{T.fullTime || "Full Time"}</option>
          <option value="part_time">{T.partTime || "Part Time"}</option>
        </select>
        <label style={{ fontSize: 12, color: "#475569", marginTop: 4 }}>
          {T.officialStartDate || "Official Start Date"} <span style={{ color: "#DC2626" }}>*</span>
          <input
            name="start_date"
            type="date"
            value={newEmp.start_date}
            onChange={handleChange}
            max={new Date().toISOString().slice(0, 10)}
            required
            style={{ display: "block", marginTop: 4, width: "100%" }}
          />
          <span style={{ fontSize: 11, color: "#94A3B8", display: "block", marginTop: 2 }}>
            {T.startDateHint || "Used to calculate when this worker becomes eligible for paid leave."}
          </span>
        </label>
        <button type="submit" disabled={loading}>
          {loading ? (T.creatingEmployee || "Creating...") : (T.createEmployee || "Create Employee")}
        </button>
      </form>
    </div>
  );
}

export default AddEmployee;
