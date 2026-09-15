import {
  FlaskConical,
  Images,
  Lightbulb,
  PanelsTopLeft,
  Server,
  Settings as SettingsIcon,
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
          <div className="nav-group">
            <p className="nav-caption">Research</p>
            <NavLink to="/ideas">
              <Lightbulb size={19} aria-hidden="true" />
              Ideas
            </NavLink>
            <NavLink to="/experiments">
              <FlaskConical size={19} aria-hidden="true" />
              Experiments
            </NavLink>
            <NavLink to="/results">
              <Images size={19} aria-hidden="true" />
              Results
            </NavLink>
          </div>
          <div className="nav-group">
            <p className="nav-caption">Workspace</p>
            <NavLink to="/models">
              <Server size={19} aria-hidden="true" />
              Models
            </NavLink>
            <NavLink to="/settings">
              <SettingsIcon size={19} aria-hidden="true" />
              Settings
            </NavLink>
          </div>
        </nav>
        <div className="sidebar-footer">
          <p className="sidebar-environment"><span className="local-dot" /> Local application</p>
          {bootstrap.active_job ? (
            <div className="sidebar-activity">
              <span>Current job</span>
              <Status state={bootstrap.active_job.state} />
            </div>
          ) : <p className="sidebar-idle">No active job.</p>}
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
