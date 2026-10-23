import { NavLink, Route, Routes } from "react-router-dom";
import Scanner from "./pages/Scanner";
import AnomalyDetailPage from "./pages/AnomalyDetail";
import DataHealthPage from "./pages/DataHealth";

const link = ({ isActive }: { isActive: boolean }) =>
  `rounded-md px-3 py-1.5 text-sm ${isActive ? "bg-slate-200 font-medium dark:bg-slate-800" : "hover:bg-slate-100 dark:hover:bg-slate-900"}`;

export default function App() {
  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-4 px-4 py-2">
          <span className="font-semibold">Quant Anomaly Lab</span>
          <nav className="flex gap-1">
            <NavLink to="/" end className={link}>
              Market Scanner
            </NavLink>
            <NavLink to="/data" className={link}>
              Data Health
            </NavLink>
          </nav>
          <span className="ml-auto text-xs text-slate-500">Research only · live execution disabled</span>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-4">
        <Routes>
          <Route path="/" element={<Scanner />} />
          <Route path="/anomalies/:id" element={<AnomalyDetailPage />} />
          <Route path="/data" element={<DataHealthPage />} />
        </Routes>
      </main>
    </div>
  );
}
