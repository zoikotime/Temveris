/**
 * useDirectory — shared hook for the offboarding UI to resolve worker_id → row.
 *
 * Wraps the `/employees` directory cache (api/directory.js) and exposes the
 * pieces every offboarding surface needs: the list, a lookup map, a label
 * helper, and the distinct-departments list (driving the Register filter).
 *
 * Each consumer gets the same cached list (single network request per minute)
 * but its own React state so re-renders are scoped.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  buildEmployeeMap, distinctDepartments, listEmployees, workerLabel,
} from "../../api/directory";

export default function useDirectory() {
  const [employees, setEmployees] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async (force = false) => {
    setLoading(true);
    setError(null);
    try {
      const list = await listEmployees({ force });
      setEmployees(list);
    } catch (e) {
      setError(e?.response?.data?.detail || e?.message || "Failed to load directory");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(false); }, [load]);

  const map = useMemo(() => buildEmployeeMap(employees), [employees]);
  const departments = useMemo(() => distinctDepartments(employees), [employees]);
  const labelFor = useCallback((id) => workerLabel(id, map), [map]);

  return { employees, map, departments, labelFor, loading, error, reload: load };
}
