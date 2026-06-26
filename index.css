import React, { useState, useEffect } from "react";
import { 
  Search, 
  Filter, 
  Download, 
  RefreshCw, 
  Calendar, 
  User, 
  Building2, 
  MapPin, 
  CheckCircle2, 
  AlertTriangle, 
  Clock, 
  Database, 
  X,
  Info
} from "lucide-react";

interface Worker {
  telegram_id: number;
  last_name: string;
  first_name: string;
  position: string;
  group_id: number;
  schedule: string;
  needs_daily_fact: number;
  sort_order: number;
  is_active: number;
  object_id: string;
}

interface Report {
  id: number;
  telegram_id: number;
  report_date: string;
  report_type: string;
  slot_time: string | null;
  received_at: string;
  is_ok: number;
  is_late: number;
  format_comment: string | null;
  required_action: string | null;
  raw_text: string;
  last_name?: string;
  first_name?: string;
  department?: string;
  object_id?: string;
}

export default function App() {
  const [reports, setReports] = useState<Report[]>([]);
  const [workers, setWorkers] = useState<Worker[]>([]);
  const [departments, setDepartments] = useState<string[]>([]);
  const [objects, setObjects] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  // Filter States
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [workerId, setWorkerId] = useState("");
  const [department, setDepartment] = useState("");
  const [objectId, setObjectId] = useState("");
  const [isOk, setIsOk] = useState("");
  const [search, setSearch] = useState("");

  // Modal State
  const [selectedReport, setSelectedReport] = useState<Report | null>(null);

  const fetchFiltersData = async () => {
    try {
      const [workersRes, deptsRes, objsRes] = await Promise.all([
        fetch("/api/workers"),
        fetch("/api/departments"),
        fetch("/api/objects")
      ]);
      if (workersRes.ok) setWorkers(await workersRes.json());
      if (deptsRes.ok) setDepartments(await deptsRes.json());
      if (objsRes.ok) setObjects(await objsRes.json());
    } catch (e) {
      console.error("Error loading filter options:", e);
    }
  };

  const fetchReports = async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (startDate) params.append("startDate", startDate);
      if (endDate) params.append("endDate", endDate);
      if (workerId) params.append("workerId", workerId);
      if (department) params.append("department", department);
      if (objectId) params.append("objectId", objectId);
      if (isOk) params.append("isOk", isOk);
      if (search) params.append("search", search);

      const res = await fetch(`/api/reports?${params.toString()}`);
      if (res.ok) {
        setReports(await res.json());
      }
    } catch (e) {
      console.error("Error fetching reports:", e);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchFiltersData();
    fetchReports();
  }, []);

  const handleRefresh = () => {
    fetchFiltersData();
    fetchReports();
  };

  const handleSearchSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    fetchReports();
  };

  const resetFilters = () => {
    setStartDate("");
    setEndDate("");
    setWorkerId("");
    setDepartment("");
    setObjectId("");
    setIsOk("");
    setSearch("");
    setTimeout(() => {
      // Small timeout to allow state updates to clear
      fetchReports();
    }, 50);
  };

  const applyQuickFilter = (type: 'today' | 'yesterday' | 'week' | 'month') => {
    const today = new Date();
    const formatDate = (date: Date) => date.toISOString().split('T')[0];

    if (type === 'today') {
      const dStr = formatDate(today);
      setStartDate(dStr);
      setEndDate(dStr);
    } else if (type === 'yesterday') {
      const yesterday = new Date();
      yesterday.setDate(today.getDate() - 1);
      const dStr = formatDate(yesterday);
      setStartDate(dStr);
      setEndDate(dStr);
    } else if (type === 'week') {
      const lastWeek = new Date();
      lastWeek.setDate(today.getDate() - 7);
      setStartDate(formatDate(lastWeek));
      setEndDate(formatDate(today));
    } else if (type === 'month') {
      const lastMonth = new Date();
      lastMonth.setDate(today.getDate() - 30);
      setStartDate(formatDate(lastMonth));
      setEndDate(formatDate(today));
    }
  };

  const exportToCSV = () => {
    if (reports.length === 0) return;

    // BOM for proper Cyrillic rendering in Excel
    const BOM = "\uFEFF";
    const headers = ["Дата", "Время", "Сотрудник", "Отдел", "Объект", "Тип отчета", "Слот", "Статус", "Комментарий", "Текст отчета"];
    const rows = reports.map(r => [
      r.report_date,
      r.received_at,
      `${r.last_name || "Удаленный"} ${r.first_name || "Сотрудник"}`,
      r.department || "Не указано",
      r.object_id || "Основной",
      r.report_type === "status" ? "Статус" : "Итог дня",
      r.slot_time || "-",
      r.is_ok ? "ОК" : "Замечание",
      r.format_comment || "",
      r.raw_text.replace(/"/g, '""')
    ]);

    const csvContent = BOM + [
      headers.join(";"),
      ...rows.map(row => row.map(val => `"${val}"`).join(";"))
    ].join("\n");

    const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.setAttribute("href", url);
    link.setAttribute("download", `reports_export_${new Date().toISOString().split('T')[0]}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  // Stats calculation
  const totalReports = reports.length;
  const okReports = reports.filter(r => r.is_ok).length;
  const okRate = totalReports > 0 ? Math.round((okReports / totalReports) * 100) : 100;
  const issueReports = totalReports - okReports;
  const lateReports = reports.filter(r => r.is_late).length;

  return (
    <div className="min-h-screen bg-slate-50 text-slate-800 font-sans" id="admin_dashboard">
      {/* Header Banner */}
      <header className="bg-slate-900 text-white shadow-md border-b border-slate-800" id="header_section">
        <div className="max-w-7xl mx-auto px-4 py-5 flex flex-col sm:flex-row justify-between items-start sm:items-center gap-4">
          <div>
            <div className="flex items-center gap-2">
              <Database className="w-6 h-6 text-emerald-400" />
              <h1 className="text-2xl font-bold tracking-tight">Прораб-Бот</h1>
            </div>
            <p className="text-slate-400 text-xs mt-1">Панель администратора • Мониторинг отчетов и дисциплины</p>
          </div>
          <div className="flex items-center gap-3">
            <button 
              onClick={handleRefresh}
              className="flex items-center gap-2 bg-slate-800 hover:bg-slate-700 text-slate-200 text-sm font-medium px-4 py-2 rounded-lg border border-slate-700 transition"
              id="refresh_btn"
            >
              <RefreshCw className="w-4 h-4" />
              Обновить
            </button>
            <button 
              onClick={exportToCSV}
              disabled={reports.length === 0}
              className="flex items-center gap-2 bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded-lg transition"
              id="export_btn"
            >
              <Download className="w-4 h-4" />
              Экспорт CSV
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-7xl mx-auto px-4 py-6" id="main_content">
        {/* KPI Cards */}
        <section className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6" id="kpi_widgets">
          <div className="bg-white p-5 rounded-xl border border-slate-200 shadow-sm flex items-center justify-between">
            <div>
              <span className="text-slate-500 text-xs font-semibold uppercase tracking-wider">Всего отчетов</span>
              <h3 className="text-3xl font-extrabold text-slate-900 mt-1">{totalReports}</h3>
              <p className="text-slate-400 text-xs mt-1">За выбранный период</p>
            </div>
            <div className="bg-blue-50 p-3 rounded-lg text-blue-600">
              <Database className="w-6 h-6" />
            </div>
          </div>

          <div className="bg-white p-5 rounded-xl border border-slate-200 shadow-sm flex items-center justify-between">
            <div>
              <span className="text-slate-500 text-xs font-semibold uppercase tracking-wider">Дисциплина (ОК)</span>
              <h3 className={`text-3xl font-extrabold mt-1 ${okRate >= 80 ? 'text-emerald-600' : 'text-amber-600'}`}>
                {okRate}%
              </h3>
              <p className="text-slate-400 text-xs mt-1">{okReports} из {totalReports} отчетов</p>
            </div>
            <div className={`p-3 rounded-lg ${okRate >= 80 ? 'bg-emerald-50 text-emerald-600' : 'bg-amber-50 text-amber-600'}`}>
              <CheckCircle2 className="w-6 h-6" />
            </div>
          </div>

          <div className="bg-white p-5 rounded-xl border border-slate-200 shadow-sm flex items-center justify-between">
            <div>
              <span className="text-slate-500 text-xs font-semibold uppercase tracking-wider">Замечания</span>
              <h3 className="text-3xl font-extrabold text-rose-600 mt-1">{issueReports}</h3>
              <p className="text-slate-400 text-xs mt-1">Требуют исправления</p>
            </div>
            <div className="bg-rose-50 p-3 rounded-lg text-rose-600">
              <AlertTriangle className="w-6 h-6" />
            </div>
          </div>

          <div className="bg-white p-5 rounded-xl border border-slate-200 shadow-sm flex items-center justify-between">
            <div>
              <span className="text-slate-500 text-xs font-semibold uppercase tracking-wider">Опоздания</span>
              <h3 className="text-3xl font-extrabold text-amber-600 mt-1">{lateReports}</h3>
              <p className="text-slate-400 text-xs mt-1">Сданы позже дедлайна</p>
            </div>
            <div className="bg-amber-50 p-3 rounded-lg text-amber-600">
              <Clock className="w-6 h-6" />
            </div>
          </div>
        </section>

        {/* Filters Panel */}
        <section className="bg-white border border-slate-200 rounded-xl shadow-sm mb-6 overflow-hidden" id="filter_panel">
          <div className="bg-slate-50 px-5 py-3 border-b border-slate-200 flex justify-between items-center">
            <span className="text-sm font-bold text-slate-700 flex items-center gap-2">
              <Filter className="w-4 h-4 text-slate-500" />
              Фильтрация и поиск
            </span>
            <button 
              onClick={resetFilters}
              className="text-xs text-slate-500 hover:text-rose-600 transition font-medium"
            >
              Сбросить фильтры
            </button>
          </div>
          <div className="p-5">
            <form onSubmit={handleSearchSubmit} className="space-y-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
                {/* Search text input */}
                <div className="col-span-1 sm:col-span-2">
                  <label className="block text-xs font-semibold text-slate-500 mb-1">Поиск</label>
                  <div className="relative">
                    <input 
                      type="text" 
                      placeholder="Поиск по тексту, замечанию, фамилии..."
                      value={search}
                      onChange={(e) => setSearch(e.target.value)}
                      className="w-full bg-slate-50 border border-slate-200 rounded-lg pl-9 pr-4 py-2 text-sm focus:outline-none focus:border-emerald-500 transition"
                    />
                    <Search className="w-4 h-4 text-slate-400 absolute left-3 top-3" />
                  </div>
                </div>

                {/* Datepicker Start */}
                <div>
                  <label className="block text-xs font-semibold text-slate-500 mb-1">Дата С</label>
                  <div className="relative">
                    <input 
                      type="date" 
                      value={startDate}
                      onChange={(e) => setStartDate(e.target.value)}
                      className="w-full bg-slate-50 border border-slate-200 rounded-lg pl-9 pr-3 py-2 text-sm focus:outline-none focus:border-emerald-500 transition"
                    />
                    <Calendar className="w-4 h-4 text-slate-400 absolute left-3 top-3" />
                  </div>
                </div>

                {/* Datepicker End */}
                <div>
                  <label className="block text-xs font-semibold text-slate-500 mb-1">Дата По</label>
                  <div className="relative">
                    <input 
                      type="date" 
                      value={endDate}
                      onChange={(e) => setEndDate(e.target.value)}
                      className="w-full bg-slate-50 border border-slate-200 rounded-lg pl-9 pr-3 py-2 text-sm focus:outline-none focus:border-emerald-500 transition"
                    />
                    <Calendar className="w-4 h-4 text-slate-400 absolute left-3 top-3" />
                  </div>
                </div>

                {/* Worker Dropdown */}
                <div>
                  <label className="block text-xs font-semibold text-slate-500 mb-1">Сотрудник</label>
                  <div className="relative">
                    <select 
                      value={workerId}
                      onChange={(e) => setWorkerId(e.target.value)}
                      className="w-full bg-slate-50 border border-slate-200 rounded-lg pl-9 pr-3 py-2 text-sm focus:outline-none focus:border-emerald-500 transition appearance-none"
                    >
                      <option value="">Все сотрудники</option>
                      {workers.map(w => (
                        <option key={w.telegram_id} value={w.telegram_id}>
                          {w.last_name} {w.first_name}
                        </option>
                      ))}
                    </select>
                    <User className="w-4 h-4 text-slate-400 absolute left-3 top-3" />
                  </div>
                </div>

                {/* Department Dropdown */}
                <div>
                  <label className="block text-xs font-semibold text-slate-500 mb-1">Отдел</label>
                  <div className="relative">
                    <select 
                      value={department}
                      onChange={(e) => setDepartment(e.target.value)}
                      className="w-full bg-slate-50 border border-slate-200 rounded-lg pl-9 pr-3 py-2 text-sm focus:outline-none focus:border-emerald-500 transition appearance-none"
                    >
                      <option value="">Все отделы</option>
                      {departments.map(d => (
                        <option key={d} value={d}>{d}</option>
                      ))}
                    </select>
                    <Building2 className="w-4 h-4 text-slate-400 absolute left-3 top-3" />
                  </div>
                </div>

                {/* Object Dropdown */}
                <div>
                  <label className="block text-xs font-semibold text-slate-500 mb-1">Объект</label>
                  <div className="relative">
                    <select 
                      value={objectId}
                      onChange={(e) => setObjectId(e.target.value)}
                      className="w-full bg-slate-50 border border-slate-200 rounded-lg pl-9 pr-3 py-2 text-sm focus:outline-none focus:border-emerald-500 transition appearance-none"
                    >
                      <option value="">Все объекты</option>
                      {objects.map(o => (
                        <option key={o} value={o}>{o}</option>
                      ))}
                    </select>
                    <MapPin className="w-4 h-4 text-slate-400 absolute left-3 top-3" />
                  </div>
                </div>

                {/* Status Dropdown */}
                <div>
                  <label className="block text-xs font-semibold text-slate-500 mb-1">Статус отчета</label>
                  <div className="relative">
                    <select 
                      value={isOk}
                      onChange={(e) => setIsOk(e.target.value)}
                      className="w-full bg-slate-50 border border-slate-200 rounded-lg pl-9 pr-3 py-2 text-sm focus:outline-none focus:border-emerald-500 transition appearance-none"
                    >
                      <option value="">Все статусы</option>
                      <option value="true">ОК (Сдан без замечаний)</option>
                      <option value="false">Замечание (Требует внимания)</option>
                    </select>
                    <CheckCircle2 className="w-4 h-4 text-slate-400 absolute left-3 top-3" />
                  </div>
                </div>
              </div>

              {/* Quick Select Buttons & Filter Trigger */}
              <div className="flex flex-wrap justify-between items-center pt-2 gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-xs text-slate-400 font-medium mr-1">Быстрый период:</span>
                  <button type="button" onClick={() => applyQuickFilter('today')} className="text-xs bg-slate-100 hover:bg-slate-200 text-slate-700 px-2.5 py-1 rounded">Сегодня</button>
                  <button type="button" onClick={() => applyQuickFilter('yesterday')} className="text-xs bg-slate-100 hover:bg-slate-200 text-slate-700 px-2.5 py-1 rounded">Вчера</button>
                  <button type="button" onClick={() => applyQuickFilter('week')} className="text-xs bg-slate-100 hover:bg-slate-200 text-slate-700 px-2.5 py-1 rounded">7 дней</button>
                  <button type="button" onClick={() => applyQuickFilter('month')} className="text-xs bg-slate-100 hover:bg-slate-200 text-slate-700 px-2.5 py-1 rounded">30 дней</button>
                </div>
                
                <button 
                  type="submit" 
                  className="bg-slate-900 hover:bg-slate-800 text-white font-medium text-sm px-6 py-2 rounded-lg shadow-sm transition"
                >
                  Применить фильтры
                </button>
              </div>
            </form>
          </div>
        </section>

        {/* Reports Table Card */}
        <section className="bg-white border border-slate-200 rounded-xl shadow-sm overflow-hidden" id="table_panel">
          <div className="overflow-x-auto">
            {loading ? (
              <div className="py-20 flex flex-col justify-center items-center gap-3 text-slate-400">
                <RefreshCw className="w-8 h-8 animate-spin text-emerald-500" />
                <span className="text-sm">Загрузка данных...</span>
              </div>
            ) : reports.length === 0 ? (
              <div className="py-20 text-center text-slate-400 flex flex-col items-center gap-2">
                <Database className="w-10 h-10 text-slate-300" />
                <p className="text-sm">По вашему запросу не найдено ни одного отчета.</p>
                <button onClick={resetFilters} className="text-xs text-emerald-600 font-bold hover:underline">Сбросить все фильтры</button>
              </div>
            ) : (
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="bg-slate-50 border-b border-slate-200 text-slate-500 text-xs font-bold uppercase tracking-wider">
                    <th className="px-6 py-3.5">Дата</th>
                    <th className="px-6 py-3.5">Сотрудник</th>
                    <th className="px-6 py-3.5">Отдел / Объект</th>
                    <th className="px-6 py-3.5">Тип / Слот</th>
                    <th className="px-6 py-3.5">Статус</th>
                    <th className="px-6 py-3.5">Опоздание</th>
                    <th className="px-6 py-3.5">Текст отчета</th>
                    <th className="px-6 py-3.5 text-right">Инфо</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 text-sm">
                  {reports.map(r => (
                    <tr key={r.id} className="hover:bg-slate-50/50 transition">
                      <td className="px-6 py-4 whitespace-nowrap">
                        <div className="font-semibold text-slate-900">{r.report_date}</div>
                        <div className="text-slate-400 text-xs">{r.received_at}</div>
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        <div className="font-semibold text-slate-900">
                          {r.last_name || "Удаленный"} {r.first_name || "Сотрудник"}
                        </div>
                        <div className="text-slate-400 text-xs">ID: {r.telegram_id}</div>
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        <div className="text-slate-700 flex items-center gap-1">
                          <Building2 className="w-3.5 h-3.5 text-slate-400" />
                          {r.department || "Не указано"}
                        </div>
                        <div className="text-slate-500 text-xs flex items-center gap-1 mt-0.5">
                          <MapPin className="w-3 h-3 text-slate-400" />
                          {r.object_id || "Основной"}
                        </div>
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${
                          r.report_type === 'status' ? 'bg-indigo-50 text-indigo-700' : 'bg-emerald-50 text-emerald-700'
                        }`}>
                          {r.report_type === 'status' ? 'Статус' : 'Итог дня'}
                        </span>
                        {r.slot_time && (
                          <div className="text-slate-400 text-xs mt-0.5 font-mono">Слот: {r.slot_time}</div>
                        )}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        {r.is_ok ? (
                          <span className="inline-flex items-center gap-1 bg-emerald-50 text-emerald-700 px-2.5 py-1 rounded-full text-xs font-bold">
                            <CheckCircle2 className="w-3.5 h-3.5" />
                            ОК
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 bg-rose-50 text-rose-700 px-2.5 py-1 rounded-full text-xs font-bold">
                            <AlertTriangle className="w-3.5 h-3.5" />
                            Замечание
                          </span>
                        )}
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        {r.is_late ? (
                          <span className="inline-flex items-center gap-1 bg-amber-50 text-amber-700 px-2 py-0.5 rounded text-xs font-medium">
                            <Clock className="w-3.5 h-3.5" />
                            Опоздание
                          </span>
                        ) : (
                          <span className="text-slate-300 text-xs">-</span>
                        )}
                      </td>
                      <td className="px-6 py-4 max-w-xs truncate text-slate-600 font-mono text-xs">
                        {r.raw_text}
                      </td>
                      <td className="px-6 py-4 text-right whitespace-nowrap">
                        <button 
                          onClick={() => setSelectedReport(r)}
                          className="text-slate-400 hover:text-slate-900 transition p-1"
                          title="Подробный анализ отчета"
                        >
                          <Info className="w-4 h-4" />
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
          {reports.length > 0 && (
            <div className="bg-slate-50 px-6 py-3 border-t border-slate-200 text-xs text-slate-400">
              Показано {reports.length} отчетов
            </div>
          )}
        </section>
      </main>

      {/* Report Analysis Modal */}
      {selectedReport && (
        <div className="fixed inset-0 bg-slate-900/40 backdrop-blur-sm flex justify-center items-center z-50 p-4" id="analysis_modal">
          <div className="bg-white w-full max-w-2xl rounded-xl shadow-xl overflow-hidden border border-slate-200 flex flex-col max-h-[90vh]">
            {/* Modal Header */}
            <div className="bg-slate-900 text-white px-6 py-4 flex justify-between items-center border-b border-slate-800">
              <div>
                <h3 className="font-bold text-lg">Анализ отчета ИИ</h3>
                <p className="text-slate-400 text-xs">Дата отчета: {selectedReport.report_date} • Получен: {selectedReport.received_at}</p>
              </div>
              <button 
                onClick={() => setSelectedReport(null)}
                className="text-slate-400 hover:text-white transition p-1"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Modal Body */}
            <div className="p-6 overflow-y-auto space-y-4">
              {/* Worker summary */}
              <div className="grid grid-cols-2 gap-4 bg-slate-50 p-4 rounded-lg text-xs">
                <div>
                  <span className="text-slate-400 block font-medium">Сотрудник</span>
                  <span className="font-bold text-slate-800 text-sm">{selectedReport.last_name} {selectedReport.first_name}</span>
                </div>
                <div>
                  <span className="text-slate-400 block font-medium">Отдел / Объект</span>
                  <span className="font-bold text-slate-800 text-sm">{selectedReport.department || "Не указано"} • {selectedReport.object_id || "Основной"}</span>
                </div>
                <div>
                  <span className="text-slate-400 block font-medium">Тип отчета</span>
                  <span className="font-bold text-slate-800">{selectedReport.report_type === 'status' ? 'Почасовой статус' : 'Факт / Итог дня'}</span>
                </div>
                {selectedReport.slot_time && (
                  <div>
                    <span className="text-slate-400 block font-medium">Временной слот</span>
                    <span className="font-bold text-slate-800 font-mono">{selectedReport.slot_time}</span>
                  </div>
                )}
              </div>

              {/* Status Badge */}
              <div className="flex items-center gap-3">
                <span className="text-slate-500 font-semibold text-xs">Оценка прораба-ИИ:</span>
                {selectedReport.is_ok ? (
                  <span className="inline-flex items-center gap-1.5 bg-emerald-50 text-emerald-800 border border-emerald-200 px-3 py-1 rounded-full text-xs font-bold">
                    <CheckCircle2 className="w-4 h-4 text-emerald-600" />
                    ОТЧЕТ ПРИНЯТ (Всё ОК)
                  </span>
                ) : (
                  <span className="inline-flex items-center gap-1.5 bg-rose-50 text-rose-800 border border-rose-200 px-3 py-1 rounded-full text-xs font-bold">
                    <AlertTriangle className="w-4 h-4 text-rose-600" />
                    ОТКЛОНЕНО (Есть замечание)
                  </span>
                )}
              </div>

              {/* Raw Text Box */}
              <div>
                <span className="text-xs font-bold text-slate-400 block mb-1">Исходное сообщение сотрудника:</span>
                <div className="bg-slate-900 text-slate-100 font-mono text-xs p-4 rounded-lg whitespace-pre-wrap border border-slate-800 shadow-inner">
                  {selectedReport.raw_text || "Нет текста"}
                </div>
              </div>

              {/* Remarks box (if any) */}
              {!selectedReport.is_ok && (
                <div className="space-y-3">
                  <div className="bg-rose-50/50 border border-rose-200 p-4 rounded-lg">
                    <span className="text-xs font-bold text-rose-700 block mb-1 flex items-center gap-1">
                      <AlertTriangle className="w-3.5 h-3.5" />
                      Выявленное замечание ИИ:
                    </span>
                    <p className="text-sm text-rose-900 font-medium">{selectedReport.format_comment || "Нет замечания"}</p>
                  </div>

                  <div className="bg-amber-50/50 border border-amber-200 p-4 rounded-lg">
                    <span className="text-xs font-bold text-amber-700 block mb-1 flex items-center gap-1">
                      <Clock className="w-3.5 h-3.5" />
                      Предпринятое действие / Оповещение:
                    </span>
                    <p className="text-sm text-amber-900 font-medium">{selectedReport.required_action || "Нет действия"}</p>
                  </div>
                </div>
              )}
            </div>

            {/* Modal Footer */}
            <div className="bg-slate-50 px-6 py-4 flex justify-end border-t border-slate-100">
              <button 
                onClick={() => setSelectedReport(null)}
                className="bg-slate-900 hover:bg-slate-800 text-white font-medium text-sm px-5 py-2 rounded-lg transition"
              >
                Закрыть
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
