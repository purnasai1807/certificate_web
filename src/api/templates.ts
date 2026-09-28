import { CertificateTemplate, TemplateFieldConfig } from '../types';
import { apiClient, ApiResponse, getAuthToken, API_BASE_URL } from './client';

export const templatesService = {
  getTemplates(): Promise<ApiResponse<CertificateTemplate[]>> {
    return apiClient('/templates');
  },

  getTemplateById(id: string): Promise<ApiResponse<CertificateTemplate>> {
    return apiClient(`/templates/${id}`);
  },

  async uploadTemplate(file: File, customName?: string): Promise<ApiResponse<CertificateTemplate>> {
    const formData = new FormData();
    formData.append('file', file);
    if (customName) formData.append('name', customName);
    return apiClient('/templates', { method: 'POST', body: formData });
  },

  setActiveTemplate(id: string) {
    return apiClient<{ templateId: string; active: boolean }>(`/templates/${id}/activate`, { method: 'POST' });
  },

  updateTemplateFields(templateId: string, fields: TemplateFieldConfig[], createVersion = true) {
    return apiClient<CertificateTemplate>(`/templates/${templateId}/fields`, {
      method: 'PUT',
      body: JSON.stringify({ fields, createVersion }),
    });
  },

  async exactPreview(templateId: string, fields: TemplateFieldConfig[], sampleData: Record<string, string>) {
    const token = getAuthToken();
    const response = await fetch(`${API_BASE_URL}/templates/${templateId}/preview`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify({ fields, sampleData }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      throw new Error(data?.error?.message || `Preview failed (${response.status})`);
    }
    return response.blob();
  },

  getVersions(id: string) {
    return apiClient<Array<{ id: string; createdAt: string; fields: TemplateFieldConfig[] }>>(`/templates/${id}/versions`);
  },

  restoreVersion(id: string, versionId: string) {
    return apiClient<CertificateTemplate>(`/templates/${id}/versions/${versionId}/restore`, { method: 'POST' });
  },

  deleteTemplate(id: string) {
    return apiClient<boolean>(`/templates/${id}`, { method: 'DELETE' });
  },
};
