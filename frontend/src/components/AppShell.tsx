import { useQuery } from '@tanstack/react-query'
import { NavLink, Outlet } from 'react-router-dom'

import { getHealth, getProviderConfig } from '../api/client'
import { StatusBadge } from './StatusBadge'

export function AppShell() {
  const health = useQuery({ queryKey: ['health'], queryFn: getHealth, retry: 1 })
  const provider = useQuery({ queryKey: ['provider-config'], queryFn: getProviderConfig, retry: 1 })

  return (
    <div className="app-frame">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <header className="app-header">
        <div className="app-header__identity">
          <NavLink to="/" className="wordmark" aria-label="心智评鉴工作台首页">
            <span className="wordmark__seal" aria-hidden="true">
              评
            </span>
            <span>
              <strong>心智评鉴工作台</strong>
              <small>初阶心理服务从业者 · 胜任力测评</small>
            </span>
          </NavLink>
          <span className="demo-stamp">量心队 · 测评工作台</span>
        </div>

        <nav className="primary-nav" aria-label="主要导航">
          <NavLink to="/">总览</NavLink>
          <NavLink to="/assessment">正式测评</NavLink>
          <NavLink to="/experience">自由体验</NavLink>
          <NavLink to="/configure">任务配置</NavLink>
          <NavLink to="/rubric">完整量规</NavLink>
        </nav>

        <div className="runtime-strip" aria-label="系统运行状态">
          <StatusBadge
            label="本地服务"
            value={health.isPending ? '检查中' : health.isSuccess ? '已连接' : '未连接'}
            tone={health.isPending ? 'waiting' : health.isSuccess ? 'ready' : 'offline'}
          />
          <StatusBadge
            label="模型与语音"
            value={provider.isPending ? '检查中' : provider.data?.configured ? '已配置' : '待配置'}
            tone={provider.data?.configured ? 'ready' : 'waiting'}
          />
        </div>
      </header>

      <main id="main-content" className="app-main">
        <Outlet />
      </main>

      <footer className="app-footer">
        <span>本系统用于竞赛展示与发展性反馈</span>
        <span>量心队 · 厚粲杯参赛作品</span>
      </footer>
    </div>
  )
}
