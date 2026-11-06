import {
  ActivityIcon,
  BoxesIcon,
  LayersIcon,
  LayoutGridIcon,
  LogOutIcon,
  ChevronsUpDownIcon,
  NetworkIcon,
  PlusCircleIcon,
  RouteIcon,
  ServerIcon,
  ShieldCheckIcon,
  Trash2Icon,
  UserIcon,
  UsersIcon,
  WorkflowIcon,
  type LucideIcon,
} from "lucide-react";
import { useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Select } from "@/components/ui/select";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import { useAuth, useSso, useCapabilities, useCertificates, useGraph, useStatus, type Session } from "./api";
import { certHealth } from "./certificateHealth";
import { Detail } from "./Detail";
import { Login } from "./Login";
import { GraphView } from "./GraphView";
import { Certificates } from "./Certificates";
import { Catalog, StatusList } from "./Lists";
import { NewApp } from "./NewApp";
import { RemoveApp } from "./RemoveApp";
import { Stacks } from "./Stacks";
import { Users } from "./Users";
import { ThemeToggle } from "./ThemeToggle";
import { buildView, defaultOptions, VIEW_GROUPS, VIEWS, type ViewId } from "./views";

const saved = (): ViewId => {
  try {
    const v = location.hash.slice(1) as ViewId;
    return VIEWS.some((x) => x.id === v) ? v : "catalog";
  } catch {
    return "catalog";
  }
};

/** The sidebar icon of each view (the names in views.ts). */
const ICONS: Record<string, LucideIcon> = {
  LayoutGrid: LayoutGridIcon,
  Activity: ActivityIcon,
  ShieldCheck: ShieldCheckIcon,
  Server: ServerIcon,
  Network: NetworkIcon,
  Route: RouteIcon,
  Workflow: WorkflowIcon,
  Layers: LayersIcon,
  PlusCircle: PlusCircleIcon,
  Trash2: Trash2Icon,
  Boxes: BoxesIcon,
  Users: UsersIcon,
};

function ViewIcon({ name }: { name: string }) {
  const Icon = ICONS[name];
  return Icon ? <Icon /> : null;
}

/** The signed-in user at the foot of the sidebar: who, a way to the account page, and sign out. */
function UserMenu({ name, go, signOut }: { name: string; go: (id: ViewId) => void; signOut: () => Promise<void> }) {
  const { isMobile } = useSidebar();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<SidebarMenuButton size="lg" />}>
        <div className="flex aspect-square size-8 items-center justify-center rounded-lg bg-sidebar-accent">
          <UserIcon />
        </div>
        <div className="grid flex-1 text-left leading-tight">
          <span className="truncate font-medium">{name}</span>
          <span className="truncate text-xs text-muted-foreground">signed in</span>
        </div>
        <ChevronsUpDownIcon className="ml-auto" />
      </DropdownMenuTrigger>
      <DropdownMenuContent side={isMobile ? "bottom" : "right"} align="end" className="min-w-56">
        <DropdownMenuLabel>{name}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={() => go("users")}>
          <UsersIcon /> Account and users
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => void signOut()}>
          <LogOutIcon /> Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function Main({ session, signOut }: { session: Session; signOut: () => Promise<void> }) {
  const graphQ = useGraph();
  const status = useStatus();
  const unifi = useCapabilities().data?.unifi;
  const certs = useCertificates().data?.certificates ?? [];
  const certProblems = certs.map(certHealth).filter((c) => c.health !== "ok");
  const certDown = certProblems.some((c) => c.health === "down");
  const [view, setView] = useState<ViewId>(saved);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [containers, setContainers] = useState(false);
  const [focus, setFocus] = useState(defaultOptions.focus);

  const graph = graphQ.data;
  // The hardware layer is optional: without a router, switch or access point (no UniFi, no inventory/network.yaml) there is no Network tab.
  const hasNetwork = !graph || graph.nodes.some((n) => ["isp", "router", "switch", "device"].includes(n.type));
  const def = VIEWS.find((v) => v.id === view)!;
  const sub = useMemo(() => (graph ? buildView(view, graph, { containers, focus }) : { nodes: [], edges: [] }), [graph, view, containers, focus]);

  const go = (id: ViewId) => {
    setView(id);
    setSelected(null);
    history.replaceState(null, "", `#${id}`);
  };

  return (
    <TooltipProvider>
      <SidebarProvider className="h-svh min-h-0">
        <Sidebar variant="inset" collapsible="icon">
          <SidebarHeader>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton size="lg" tooltip="homegraf" onClick={() => go("catalog")}>
                  <div className="flex aspect-square size-8 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground">
                    <NetworkIcon />
                  </div>
                  <div className="grid flex-1 text-left leading-tight">
                    <span className="truncate font-semibold">homegraf</span>
                    <span className="truncate text-xs text-muted-foreground" title="version">
                      {__APP_VERSION__}
                    </span>
                  </div>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarHeader>
          <SidebarContent>
            {VIEW_GROUPS.map((group) => {
              const views = VIEWS.filter((v) => v.group === group && (v.id !== "network" || hasNetwork));
              return (
                <SidebarGroup key={group}>
                  <SidebarGroupLabel>{group}</SidebarGroupLabel>
                  <SidebarGroupContent>
                    <SidebarMenu aria-label={group}>
                      {views.map((v) => (
                        <SidebarMenuItem key={v.id}>
                          <SidebarMenuButton isActive={v.id === view} tooltip={v.hint} onClick={() => go(v.id)}>
                            <ViewIcon name={v.icon} />
                            <span>{v.label}</span>
                          </SidebarMenuButton>
                        </SidebarMenuItem>
                      ))}
                    </SidebarMenu>
                  </SidebarGroupContent>
                </SidebarGroup>
              );
            })}
          </SidebarContent>
          <SidebarFooter>
            <SidebarMenu>
              <SidebarMenuItem>
                <UserMenu name={session.name} go={go} signOut={signOut} />
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarFooter>
          <SidebarRail />
        </Sidebar>

        <SidebarInset className="min-h-0 min-w-0 overflow-hidden">
          <header className="flex flex-wrap items-center gap-3 border-b bg-card px-4 py-2">
            <SidebarTrigger className="-ml-2" />
            <div className="ml-auto flex flex-wrap items-center gap-3">
              <Input type="search" className="w-56" placeholder="Search…" value={query} onChange={(e) => setQuery(e.target.value)} />
              {certProblems.length > 0 && (
                <button
                  type="button"
                  className="cursor-pointer border-0 bg-transparent p-0"
                  onClick={() => go("certificates")}
                  title={`${certProblems.length} of ${certs.length} certificates are broken or due for renewal: open the list`}
                >
                  <Badge variant="outline" className={certDown ? "border-down text-down" : "border-warn text-warn"}>
                    {certProblems.length} certificate{certProblems.length === 1 ? "" : "s"} {certDown ? "broken" : "due"}
                  </Badge>
                </button>
              )}
              {unifi?.enabled && (
                <Badge
                  variant="outline"
                  className={unifi.fetched ? "border-ok text-ok" : "border-warn text-warn"}
                  title={unifi.error ?? `network read from the UniFi controller at ${unifi.fetched ?? "…"}`}
                >
                  {unifi.fetched ? "UniFi live" : "UniFi unavailable"}
                </Badge>
              )}
              <Badge
                variant="outline"
                className={status.source === "komodo" ? "border-ok text-ok" : status.source === "error" ? "border-warn text-warn" : "text-muted-foreground"}
                title={status.error ?? ""}
              >
                {status.source === "disabled"
                  ? "status off"
                  : status.source === "error"
                    ? "status stale"
                    : `live ${status.updated ? new Date(status.updated).toLocaleTimeString() : "…"}`}
              </Badge>
              <ThemeToggle />
            </div>
          </header>

          {def.kind === "graph" && (
            <div className="toolbar">
              <span className="muted">{def.hint}</span>
              {(view === "hosting" || view === "dependencies") && (
                <label className="check">
                  <input type="checkbox" checked={containers} onChange={(e) => setContainers(e.target.checked)} /> containers
                </label>
              )}
              {view === "platform" && graph && (
                <label className="check">
                  show dependents of
                  <Select
                    size="sm"
                    value={focus}
                    onValueChange={setFocus}
                    options={graph.nodes.filter((n) => n.type === "infra").map((n) => ({ value: n.id, label: n.label }))}
                  />
                </label>
              )}
            </div>
          )}

          <div className="workspace">
            <div className="canvas">
              {graphQ.isPending && <p className="empty">Building the graph…</p>}
              {graphQ.isError && <p className="warning">{graphQ.error.message}</p>}
              {graph && view === "catalog" && <Catalog graph={graph} status={status} query={query} selected={selected} onSelect={setSelected} />}
              {graph && view === "status" && <StatusList graph={graph} status={status} onSelect={setSelected} />}
              {graph && view === "certificates" && <Certificates onSelect={setSelected} />}
              {graph && view === "new" && <NewApp graph={graph} />}
              {view === "remove" && <RemoveApp />}
              {view === "stacks" && <Stacks session={session} />}
              {view === "users" && <Users session={session} />}
              {graph && def.kind === "graph" && <GraphView sub={sub} status={status} query={query} selected={selected} onSelect={setSelected} />}
            </div>
            {graph && selected && <Detail graph={graph} status={status} id={selected} onSelect={setSelected} onClose={() => setSelected(null)} />}
          </div>

          {graph && graph.warnings.length > 0 && (
            <footer className="warning">
              {graph.warnings.length} graph warning(s): {graph.warnings[0]}
            </footer>
          )}
        </SidebarInset>
      </SidebarProvider>
    </TooltipProvider>
  );
}

/** Nothing but the login page for somebody who is not signed in: the map, the status and every other route are behind the session. */
export function App() {
  const sso = useSso();
  const { session, signOut } = useAuth();
  if (sso.isPending) return <p className="empty">Loading…</p>;
  if (sso.isError) return <p className="warning">{sso.error.message}</p>;
  if (!session) return <Login />;
  return <Main session={session} signOut={signOut} />;
}
