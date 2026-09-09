import {
  createContext,
  useContext,
  useEffect,
  type ReactNode,
} from "react";
import { errorMessage, setRequestToken, useApi, type Bootstrap } from "./api";

interface Studio {
  bootstrap: Bootstrap;
  refreshBootstrap: () => void;
}
const StudioContext = createContext<Studio | null>(null);
export function useStudio() {
  const value = useContext(StudioContext);
  if (!value) throw new Error("Studio context is not available");
  return value;
}
export function StudioProvider({ children }: { children: ReactNode }) {
  const { data, error, refresh } = useApi<Bootstrap>("/api/bootstrap", 10000);
  useEffect(() => {
    if (data) setRequestToken(data.request_token);
  }, [data]);
  if (!data)
    return (
      <main className="startup">
        <h1>AI-Scientist Studio</h1>
        {error ? (
          <>
            <p role="alert">{errorMessage(error)}</p>
            <button onClick={refresh}>Reconnect</button>
          </>
        ) : (
          <p role="status">Connecting to the local application…</p>
        )}
      </main>
    );
  return (
    <StudioContext.Provider
      value={{
        bootstrap: data,
        refreshBootstrap: refresh,
      }}
    >
      {error ? (
        <div className="connection-banner" role="status">
          Connection lost—reconnecting. Saved work stays on this PC.
        </div>
      ) : null}
      {children}
    </StudioContext.Provider>
  );
}
