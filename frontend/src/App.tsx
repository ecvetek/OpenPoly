import {
  BrowserRouter,
  HashRouter,
  Navigate,
  Route,
  Routes,
  useParams,
} from 'react-router-dom'
import { AppShell } from './components/AppShell'
import { CanvasPage } from './routes/CanvasPage'
import { HealthPage } from './routes/HealthPage'
import { OverviewPage } from './routes/OverviewPage'
import { PositionsPage } from './routes/PositionsPage'
import { NewsPage } from './routes/NewsPage'
import { StatisticsPage } from './routes/StatisticsPage'
import { PositionsTab } from './routes/activity/PositionsTab'
import { PositionDetail } from './routes/activity/PositionDetail'

// Demo opens from file:// where path-based routing has no server to fall back
// on — HashRouter keeps every route resolvable (index.html#/strategy). Normal
// builds pin __DEMO__ to false, so this folds to BrowserRouter.
const Router = __DEMO__ ? HashRouter : BrowserRouter

// Activity's position-detail route carried a :positionId param; a plain
// <Navigate> can't thread that through, so it needs its own redirect.
function PositionDetailRedirect() {
  const { positionId } = useParams()
  return <Navigate to={`/positions/${positionId}`} replace />
}

function App() {
  return (
    <Router>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<Navigate to="/strategy" replace />} />
          <Route path="strategy" element={<CanvasPage />} />
          {/* "canvas" was the earlier name for this page; redirect stale links. */}
          <Route path="canvas" element={<Navigate to="/strategy" replace />} />
          <Route path="overview" element={<OverviewPage />} />
          <Route path="positions" element={<PositionsPage />}>
            <Route index element={<PositionsTab />} />
            <Route path=":positionId" element={<PositionDetail />} />
          </Route>
          <Route path="news" element={<NewsPage />} />
          {/* Activity was split into top-level Positions / News tabs; redirect
              any stale links to their new homes. */}
          <Route path="activity" element={<Navigate to="/positions" replace />} />
          <Route path="activity/overview" element={<Navigate to="/overview" replace />} />
          <Route path="activity/positions" element={<Navigate to="/positions" replace />} />
          <Route path="activity/positions/:positionId" element={<PositionDetailRedirect />} />
          <Route path="activity/news" element={<Navigate to="/news" replace />} />
          <Route path="statistics" element={<StatisticsPage />} />
          <Route path="health" element={<HealthPage />} />
          {/* "runs" / "portfolio" were earlier names for this tab; redirect
              any stale link. */}
          <Route path="runs" element={<Navigate to="/overview" replace />} />
          <Route path="portfolio" element={<Navigate to="/overview" replace />} />
          {/* Setting page removed (v9 / SK2) — secrets are managed from the
              Strategy page "Keys" drawer. Any stale link falls back there. */}
          <Route path="*" element={<Navigate to="/strategy" replace />} />
        </Route>
      </Routes>
    </Router>
  )
}

export default App
