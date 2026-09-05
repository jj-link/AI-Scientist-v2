import {
  FlaskConical,
  Images,
  Lightbulb,
  PanelsTopLeft,
  Server,
} from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";
import { StudioProvider, useStudio } from "./studio";
import { Status } from "./components";

function Shell() {
  const { bootstrap } = useStudio();
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <PanelsTopLeft size={24} aria-hidden="true" />
          </span>
          <span>
            AI-Scientist<strong>Studio</strong>
          </span>
        </div>
        <nav aria-label="Primary">
          <NavLink to="/ideas">
            <Lightbulb size={20} aria-hidden="true" />
            Ideas
          </NavLink>
          <NavLink to="/experiments">
            <FlaskConical size={20} aria-hidden="true" />
            Experiments
          </NavLink>
          <NavLink to="/results">
            <Images size={20} aria-hidden="true" />
            Results
          </NavLink>
          <NavLink to="/models">
            <Server size={20} aria-hidden="true" />
            Models
          </NavLink>
        </nav>
        <div className="sidebar-footer">
          <span className="local-dot" /> Local application
          <p>Research runs on this PC.</p>
          {bootstrap.active_job && (
            <Status state={bootstrap.active_job.state} />
          )}
        </div>
      </aside>
      <main id="main-content" className="main-content" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
export default function App() {
  return (
    <StudioProvider>
      <Shell />
    </StudioProvider>
  );
}
