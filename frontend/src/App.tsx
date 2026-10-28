import { NavLink, Route, Routes } from "react-router-dom";
import Scanner from "./pages/Scanner";
import AnomalyDetailPage from "./pages/AnomalyDetail";
import DataHealthPage from "./pages/DataHealth";
import TestsPage from "./pages/Tests";
import ResearchPage from "./pages/Research";
import HypothesisDetailPage from "./pages/HypothesisDetail";
import ValidationReport from "./pages/ValidationReport";
import AlphaResearchPage from "./pages/AlphaResearch";
import AlphaHypothesisPage from "./pages/AlphaHypothesis";
import ModelLabPage from "./pages/ModelLab";
import PortfolioPage from "./pages/Portfolio";
import PaperTradingPage from "./pages/PaperTrading";
import PaperValidationPage from "./pages/PaperValidation";
import ExperimentLabPage from "./pages/ExperimentLab";
import RobustnessLabPage from "./pages/RobustnessLab";

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
            <NavLink to="/tests" className={link}>
              Tests
            </NavLink>
            <NavLink to="/research" className={link}>
              Research Lab
            </NavLink>
            <NavLink to="/alpha" className={link}>
              Alpha Research
            </NavLink>
            <NavLink to="/models" className={link}>
              Model Lab
            </NavLink>
            <NavLink to="/lab" className={link}>
              Experiment Lab
            </NavLink>
            <NavLink to="/robustness" className={link}>
              Robustness Lab
            </NavLink>
            <NavLink to="/portfolio" className={link}>
              Portfolio &amp; Risk
            </NavLink>
            <NavLink to="/paper" className={link}>
              Paper Trading
            </NavLink>
            <NavLink to="/validation" className={link}>
              Paper Validation
            </NavLink>
            <NavLink to="/data" className={link}>
              Data Health
            </NavLink>
          </nav>
          <span className="ml-auto text-xs text-slate-500">Research and simulation only · live execution disabled</span>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-4">
        <Routes>
          <Route path="/" element={<Scanner />} />
          <Route path="/anomalies/:id" element={<AnomalyDetailPage />} />
          <Route path="/data" element={<DataHealthPage />} />
          <Route path="/tests" element={<TestsPage />} />
          <Route path="/research" element={<ResearchPage />} />
          <Route path="/research/:id" element={<HypothesisDetailPage />} />
          <Route path="/validations/:id" element={<ValidationReport />} />
          <Route path="/alpha" element={<AlphaResearchPage />} />
          <Route path="/alpha/:id" element={<AlphaHypothesisPage />} />
          <Route path="/models" element={<ModelLabPage />} />
          <Route path="/lab" element={<ExperimentLabPage />} />
          <Route path="/robustness" element={<RobustnessLabPage />} />
          <Route path="/portfolio" element={<PortfolioPage />} />
          <Route path="/paper" element={<PaperTradingPage />} />
          <Route path="/validation" element={<PaperValidationPage />} />
        </Routes>
      </main>
    </div>
  );
}
