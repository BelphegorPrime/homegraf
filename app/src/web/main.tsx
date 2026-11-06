import { QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./index.css";

// The preset themes with a .dark class on <html>: follow the system, a saved choice wins (see ThemeToggle).
try {
  const saved = localStorage.getItem("homegraf-theme");
  document.documentElement.classList.toggle("dark", saved ? saved === "dark" : matchMedia("(prefers-color-scheme: dark)").matches);
} catch {
  document.documentElement.classList.toggle("dark", matchMedia("(prefers-color-scheme: dark)").matches);
}

// A read the server answers with 401 means the session ended (a restart, a removed user): ask who is signed in again, which shows the login page.
const client: QueryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error, query) => {
      if ((error as { status?: number }).status === 401 && query.queryKey[0] !== "sso") void client.invalidateQueries({ queryKey: ["sso"] });
    },
  }),
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
