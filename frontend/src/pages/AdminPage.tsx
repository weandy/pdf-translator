import { AdminLayout, type AdminSectionKey } from "@/pages/admin/AdminLayout";
import { AdminLoginPage } from "@/pages/admin/AdminLoginPage";
import { OverviewPage } from "@/pages/admin/OverviewPage";
import { RecordsPage } from "@/pages/admin/RecordsPage";
import { ServicesPage } from "@/pages/admin/ServicesPage";
import { SettingsPage } from "@/pages/admin/SettingsPage";

export interface AdminPageProps {
  adminPath?: string;
  section?: AdminSectionKey;
  submissionAuditEnabled?: boolean;
}

export { AdminLoginPage };

export function AdminConsolePage({
  adminPath = "/weandy",
  section = "overview",
  submissionAuditEnabled = true,
}: AdminPageProps) {
  return (
    <AdminLayout
      adminPath={adminPath}
      section={section}
      submissionAuditEnabled={submissionAuditEnabled}
    >
      {section === "overview" && <OverviewPage />}
      {section === "records" && <RecordsPage />}
      {section === "services" && <ServicesPage />}
      {section === "settings" && <SettingsPage />}
    </AdminLayout>
  );
}

/** 保留旧引用兼容：直达翻译记录页面。 */
export function AdminSubmissionsPage({
  adminPath = "/weandy",
  submissionAuditEnabled = true,
}: AdminPageProps) {
  return (
    <AdminConsolePage
      adminPath={adminPath}
      section="records"
      submissionAuditEnabled={submissionAuditEnabled}
    />
  );
}
