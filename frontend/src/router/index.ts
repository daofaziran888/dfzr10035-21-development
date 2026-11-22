import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import CollectionGapView from '@/views/CollectionGapView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 采集异常组有独立复核工作台，必须排在通用模块路由之前。
    { path: '/collection_gap', name: 'collection_gap', component: CollectionGapView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
