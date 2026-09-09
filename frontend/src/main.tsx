import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import {
  createBrowserRouter,
  Navigate,
  RouterProvider,
} from "react-router-dom";
import App from "./App";
import Ideas from "./screens/Ideas";
import ExperimentSetup from "./screens/ExperimentSetup";
import Experiments from "./screens/Experiments";
import Results from "./screens/Results";
import Models from "./screens/Models";
import Settings from "./screens/Settings";
import "./styles.css";

const router = createBrowserRouter([
  {
    element: <App />,
    children: [
      { path: "/", element: <Navigate to="/ideas" replace /> },
      { path: "/ideas", element: <Ideas /> },
      { path: "/ideas/:ideaId", element: <Ideas /> },
      { path: "/ideas/:ideaId/setup", element: <ExperimentSetup /> },
      { path: "/experiments", element: <Experiments /> },
      { path: "/experiments/:jobId", element: <Experiments /> },
      { path: "/results", element: <Results /> },
      { path: "/results/:runId", element: <Results /> },
      { path: "/models", element: <Models /> },
      { path: "/settings", element: <Settings /> },
      {
        path: "*",
        element: (
          <section>
            <h1>Page not found</h1>
            <a href="/ideas">Go to Ideas</a>
          </section>
        ),
      },
    ],
  },
]);
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <RouterProvider router={router} />
  </StrictMode>,
);
