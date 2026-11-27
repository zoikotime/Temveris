import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useLanguage } from "../context/LanguageContext";
import { useTheme } from "../context/ThemeContext";
import TopNavbar from "../components/TopNavbar";
import AdminSidebar from "../components/AdminSidebar";
import API from "../api/axios";

function MyTeam() {
  const { T } = useLanguage();
  const { C } = useTheme();
  const navigate = useNavigate();
  const [employees, setEmployees] = useState([]);

  const fetchData = () => {
    API.get("/employees/team")
      .then((res) => setEmployees(Array.isArray(res.data) ? res.data : []))
      .catch(() => {});
  };

  useEffect(() => {
    fetchData();

    // Refresh every 2 minutes, and only when the tab is visible.
    const interval = setInterval(() => {
      if (!document.hidden) fetchData();
    }, 120000);
    const onVis = () => { if (!document.hidden) fetchData(); };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  const getColor = (status) => {
    if (status === "Currently Working") return "#E8F7EE"; // logo green tint
    if (status === "On Break") return "#fff3cd"; // yellow
    if (status === "Stopped Work") return "#f8d7da"; // red
    return "#e2e3e5"; // gray
  };

  return (
    <div style={{ display: "flex", height: "100vh", background: C.bg, fontFamily: "'DM Sans', system-ui, sans-serif" }}>
      <AdminSidebar activeKey="myteam" />
      <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, overflow: "hidden" }}>
        <TopNavbar
          title={T.myTeam || "My Team"}
          subtitle={T.myTeamSubtitle || "Live status of every teammate"}
          onBack={() => navigate(-1)}
        />
        <main style={{ flex: 1, overflow: "auto", padding: 20 }}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 15 }}>
            {employees.map((emp) => (
              <div
                key={emp.id}
                style={{
                  background: getColor(emp.status),
                  padding: 15,
                  borderRadius: 10,
                  width: 220,
                  boxShadow: "0 2px 5px rgba(0,0,0,0.1)",
                }}
              >
                <h3>{emp.name}</h3>
                <p>{emp.email}</p>
                <strong>{emp.status}</strong>
              </div>
            ))}
          </div>
        </main>
      </div>
    </div>
  );
}

export default MyTeam;