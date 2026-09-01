import { createBrowserRouter, type RouteObject } from 'react-router-dom'

import { AppShell } from '../components/AppShell'
import { CompletionPage } from '../pages/CompletionPage'
import { ConfigurePage } from '../pages/ConfigurePage'
import { HomePage } from '../pages/HomePage'
import { PlaceholderPage } from '../pages/PlaceholderPage'
import { StartPage } from '../pages/StartPage'
import { DeviceCheckPage } from '../pages/DeviceCheckPage'
import { SessionPage } from '../pages/SessionPage'
import { WorkRecordPage } from '../pages/WorkRecordPage'
import { ReportPage } from '../pages/ReportPage'
import { ReportProgressPage } from '../pages/ReportProgressPage'
import { RubricPage } from '../pages/RubricPage'

export const appRoutes: RouteObject[] = [
  {
    path: '/',
    element: <AppShell />,
    children: [
      { index: true, element: <HomePage /> },
      {
        path: 'assessment',
        element: <StartPage mode="assessment" />,
      },
      {
        path: 'experience',
        element: <StartPage mode="experience" />,
      },
      { path: 'device-check', element: <DeviceCheckPage /> },
      { path: 'session/:sessionId', element: <SessionPage /> },
      { path: 'sessions/:sessionId/work-record', element: <WorkRecordPage /> },
      { path: 'sessions/:sessionId/complete', element: <CompletionPage /> },
      { path: 'report-jobs/:jobId', element: <ReportProgressPage /> },
      { path: 'reports/:reportId', element: <ReportPage /> },
      { path: 'configure', element: <ConfigurePage /> },
      { path: 'rubric', element: <RubricPage /> },
      {
        path: '*',
        element: (
          <PlaceholderPage
            archiveId="404"
            title="档案未找到"
            description="这个入口尚未归档，或地址已经变更。"
            nextStep="请返回总览选择现有入口。"
          />
        ),
      },
    ],
  },
]

export const router = createBrowserRouter(appRoutes)
