document.addEventListener("DOMContentLoaded", () => {
  initScheduleCalculations();
});

function initScheduleCalculations() {
  const shiftMetricsNode = document.getElementById("shift-metrics-data");
  const scheduleTable = document.querySelector(".schedule-table");
  const scheduleForm = document.querySelector('[data-schedule-form="true"]');
  if (!shiftMetricsNode || !scheduleTable || !scheduleForm) {
    return;
  }

  const shiftMetrics = JSON.parse(shiftMetricsNode.textContent);
  const nightStart = scheduleTable.dataset.nightStart || "19:00";
  const defaultWeeklyHours = parseFloat(scheduleTable.dataset.defaultWeeklyHours || "0");
  const defaultDailyMax = parseFloat(scheduleTable.dataset.defaultDailyMax || "0");
  const defaultBaseWorkDays = Number.parseInt(scheduleTable.dataset.defaultBaseWorkDays || "6", 10) || 6;
  const allowedIncompleteDifference = Number.parseFloat(
    scheduleTable.dataset.allowedIncompleteDifference || "1",
  ) || 1;
  const programmingIntervalMinutes = Number.parseInt(
    scheduleTable.dataset.programmingIntervalMinutes || "30",
    10,
  ) || 30;
  const showNightHours = scheduleTable.dataset.showNightHours === "true";
  const showDetailedAlerts = scheduleTable.dataset.showDetailedAlerts === "true";
  const scheduleClosed = scheduleTable.dataset.scheduleClosed === "true";
  const moneyHourModes = new Set(["pay_money", "pay_money_hours"]);
  const moneyDayModes = new Set(["pay_money_day"]);
  const advanceDayModes = new Set(["advance_day"]);
  const companyDayRepaymentMode = "repay_company_day";
  const compensationModesWithHours = new Set(["pay_hours", ...moneyHourModes]);
  const restShiftLabels = new Set(["descanso"]);
  const leaveShiftLabels = new Set([
    "contratacion",
    "festivo",
    "incapacidad",
    "traslado",
    "vacaciones",
    "volante",
    "volantes",
    "renuncia",
    "licencia",
  ]);
  const loanShiftLabels = new Set(["prestamo"]);
  const absenceShiftLabels = new Set(["inasistencia"]);
  const autosaveEnabled = scheduleForm.dataset.autosaveEnabled === "true";
  const csrfToken = scheduleForm.querySelector('input[name="csrfmiddlewaretoken"]')?.value || "";
  let autosaveDirty = false;
  let autosaveInFlight = false;
  let manualSubmitInProgress = false;
  let lastInteractionAt = Date.now();

  const parseDecimal = (value) => {
    const normalized = String(value ?? "").trim().replace(",", ".");
    const parsed = Number.parseFloat(normalized);
    return Number.isFinite(parsed) ? parsed : 0;
  };

  const roundHours = (value) => Math.round((Number(value) + Number.EPSILON) * 100) / 100;
  const clampNonNegative = (value) => Math.max(roundHours(value), 0);

  const formatHours = (value, suffix = false) => {
    const rounded = roundHours(value);
    const formatted = Number.isInteger(rounded) ? String(rounded) : String(rounded).replace(/\.?0+$/, "");
    return suffix ? `${formatted} h` : formatted;
  };

  const formatBalanceHours = (value, suffix = false) => formatHours(value, suffix);

  const describeDayBalance = (value) => {
    const rounded = roundHours(value);
    if (rounded > 0.001) {
      return `${formatHours(rounded)} dia(s) a favor del trabajador`;
    }
    if (rounded < -0.001) {
      return `${formatHours(Math.abs(rounded))} dia(s) a favor de la empresa`;
    }
    return "Sin dias pendientes";
  };

  const describeWeeklyDifference = (value) => {
    const rounded = roundHours(value);
    if (rounded > 0.001) {
      return `${formatHours(rounded, true)} de excedente`;
    }
    if (rounded < -0.001) {
      return `${formatHours(Math.abs(rounded), true)} pendientes`;
    }
    return "sin diferencia horaria";
  };

  const isNonBlockingHourDifference = (validationStatus, weeklyHourDifference) => (
    ["INCOMPLETA_CORREGIBLE", "EXCESO_PROGRAMADO"].includes(validationStatus)
    && Math.abs(roundHours(weeklyHourDifference)) <= allowedIncompleteDifference + 0.001
  );

  const doesStatusBlockTransition = (validationStatus, weeklyHourDifference) => {
    if (validationStatus === "INCOMPLETA_CORREGIBLE") {
      return Math.abs(roundHours(weeklyHourDifference)) > allowedIncompleteDifference + 0.001;
    }
    return ["IMPOSIBLE_POR_CAPACIDAD", "INCONSISTENTE"].includes(validationStatus);
  };

  const getStatusBlockerMessage = (validationStatus, weeklyHourDifference) => {
    if (validationStatus === "INCOMPLETA_CORREGIBLE") {
      return `Bloquea revision/publicacion: tiene una diferencia de ${formatHours(Math.abs(weeklyHourDifference), true)}. Modifica la programacion o agrega horas pagas y guarda.`;
    }
    if (validationStatus === "IMPOSIBLE_POR_CAPACIDAD") {
      return "Bloquea revision/publicacion: la jornada ajustada no cabe en la capacidad disponible. Revisa turnos, novedades o parametrizacion del cargo y guarda.";
    }
    if (validationStatus === "INCONSISTENTE") {
      return "Bloquea revision/publicacion: hay una configuracion inconsistente. Revisa pagos, descansos, saldos o turnos y guarda.";
    }
    return "";
  };

  const getDisplayValidationStatus = (validationStatus, weeklyHourDifference) => (
    isNonBlockingHourDifference(validationStatus, weeklyHourDifference)
      ? "VALIDA CON DIFERENCIA PERMITIDA"
      : validationStatus
  );

  const toMinutes = (timeValue) => {
    const [hours, minutes] = timeValue.split(":").map((item) => Number.parseInt(item, 10));
    return hours * 60 + minutes;
  };

  const hoursToMinutes = (hoursValue) => Math.round(parseDecimal(hoursValue) * 60);
  const minutesToHours = (minutesValue) => roundHours(Number(minutesValue || 0) / 60);
  const roundMinutesToInterval = (minutesValue, intervalValue = programmingIntervalMinutes) => {
    const safeInterval = Math.max(Number(intervalValue || 0), 1);
    const normalizedMinutes = Math.max(Number(minutesValue || 0), 0);
    const quotient = Math.floor(normalizedMinutes / safeInterval);
    const remainder = normalizedMinutes - (quotient * safeInterval);
    return remainder >= safeInterval / 2
      ? (quotient + 1) * safeInterval
      : quotient * safeInterval;
  };
  const formatMinutesDuration = (minutesValue) => {
    const normalizedMinutes = Math.max(Math.round(Number(minutesValue || 0)), 0);
    const hours = Math.floor(normalizedMinutes / 60);
    const minutes = normalizedMinutes % 60;
    return `${hours}:${String(minutes).padStart(2, "0")}`;
  };
  const formatSignedMinutesDuration = (minutesValue) => {
    const normalizedMinutes = Math.round(Number(minutesValue || 0));
    const sign = normalizedMinutes < 0 ? "-" : normalizedMinutes > 0 ? "+" : "";
    return `${sign}${formatMinutesDuration(Math.abs(normalizedMinutes))}`;
  };

  const setSelectValue = (select, desiredValue) => {
    if (!select) {
      return;
    }
    const options = Array.from(select.options);
    const exact = options.find((option) => option.value === desiredValue);
    if (exact) {
      select.value = exact.value;
      return;
    }
    const normalized = String(desiredValue || "").trim().toLowerCase();
    const caseInsensitive = options.find((option) => option.value.trim().toLowerCase() === normalized);
    if (caseInsensitive) {
      select.value = caseInsensitive.value;
    }
  };

  const normalizeShiftKey = (value) => String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .trim()
    .toLowerCase();

  const parseRangeMetrics = (label) => {
    const match = String(label || "").trim().match(/^(\d{1,2}:\d{2})-(\d{1,2}:\d{2})$/);
    if (!match) {
      return { hours: 0, night_hours: 0 };
    }

    let startMinutes = toMinutes(match[1]);
    let endMinutes = toMinutes(match[2]);
    if (endMinutes <= startMinutes) {
      endMinutes += 24 * 60;
    }

    const totalHours = (endMinutes - startMinutes) / 60;
    const nightStartMinutes = toMinutes(nightStart);
    const nightHours = endMinutes <= nightStartMinutes
      ? 0
      : Math.max(endMinutes - Math.max(startMinutes, nightStartMinutes), 0) / 60;

    return {
      hours: totalHours,
      night_hours: nightHours,
    };
  };

  const getShiftNonWorkCategory = (label) => {
    const normalized = normalizeShiftKey(label);
    if (!normalized) {
      return "";
    }
    if (restShiftLabels.has(normalized)) {
      return "rest";
    }
    if (loanShiftLabels.has(normalized)) {
      return "loan";
    }
    if (absenceShiftLabels.has(normalized)) {
      return "absence";
    }
    if (leaveShiftLabels.has(normalized)) {
      return "leave";
    }
    return "";
  };

  const getWeeklyRestDayIndex = (dayStates) => {
    const sundayState = dayStates.find((dayState) => dayState.dayIndex === 0);
    if (!sundayState || sundayState.dailyHours <= 0.001) {
      return 0;
    }

    for (const dayState of dayStates) {
      if (dayState.dayIndex === 0) {
        continue;
      }
      if (dayState.dailyHours > 0.001) {
        continue;
      }
      const isNonWorkedHoliday = String(dayState.specialDayLabel || "").includes("Festivo");
      if (
        isNonWorkedHoliday
        || dayState.modeValue === "pay_day"
        || advanceDayModes.has(dayState.modeValue)
        || dayState.shiftCategories.has("rest")
      ) {
        return dayState.dayIndex;
      }
    }

    return 0;
  };

  const buildExpectedPlan = (dayStates, weeklyTargetValue, baseWorkDaysValue, scopeIndexes = null) => {
    const mandatoryRestIndex = getWeeklyRestDayIndex(dayStates);
    const expectedIndexes = [];
    const weeklyTarget = roundHours(weeklyTargetValue);
    const weeklyTargetMinutes = hoursToMinutes(weeklyTarget);
    const baseWorkDays = Math.max(Number.parseInt(baseWorkDaysValue || defaultBaseWorkDays, 10) || defaultBaseWorkDays, 1);
    const provisionalStates = dayStates.map((dayState) => {
      const isHoliday = String(dayState.specialDayLabel || "").includes("Festivo") && dayState.dayIndex !== 0;
      const isNonWorkedHoliday = isHoliday && dayState.dailyHours <= 0.001;
      const externalLoanHours = roundHours(dayState.externalLoanHours || 0);
      const isLoanDay =
        dayState.dailyHours <= 0.001
        && dayState.shiftCategories.has("loan");
      const isUnlinkedLoanDay = isLoanDay && externalLoanHours <= 0.001;
      const isAbsenceDay =
        dayState.dailyHours <= 0.001
        && dayState.shiftCategories.has("absence")
        && dayState.modeValue !== "pay_day"
        && !advanceDayModes.has(dayState.modeValue);
      const isLeaveDay =
        dayState.dailyHours <= 0.001
        && dayState.shiftCategories.has("leave")
        && !isNonWorkedHoliday
        && dayState.modeValue !== "pay_day"
        && !advanceDayModes.has(dayState.modeValue);
      const isAdditionalRestDay =
        dayState.dailyHours <= 0.001
        && dayState.shiftCategories.has("rest")
        && dayState.dayIndex !== mandatoryRestIndex
        && dayState.modeValue !== "pay_day"
        && !advanceDayModes.has(dayState.modeValue)
        && !isNonWorkedHoliday;

      let expectedReason = "laborable";
      if (scopeIndexes && !scopeIndexes.has(dayState.dayIndex)) {
        expectedReason = "fuera_de_rango";
      } else if (dayState.dayIndex === mandatoryRestIndex) {
        expectedReason = "descanso_obligatorio";
      } else if (dayState.modeValue === "pay_day") {
        expectedReason = "descanso_compensatorio";
      } else if (advanceDayModes.has(dayState.modeValue)) {
        expectedReason = "descanso_adelantado";
      } else if (isAdditionalRestDay) {
        expectedReason = "descanso_adicional";
      } else if (isNonWorkedHoliday) {
        expectedReason = "festivo_no_trabajado";
      } else if (isUnlinkedLoanDay) {
        expectedReason = "prestamo_sin_destino";
      } else if (isLeaveDay) {
        expectedReason = "novedad_no_laborable";
      }
      if (expectedReason === "laborable") {
        expectedIndexes.push(dayState.dayIndex);
      }

      return {
        ...dayState,
        isHoliday,
        isNonWorkedHoliday,
        isLeaveDay,
        isLoanDay,
        isUnlinkedLoanDay,
        externalLoanHours,
        isAbsenceDay,
        isMandatoryRestDay: dayState.dayIndex === mandatoryRestIndex,
        isAdditionalRestDay,
        expectedReason,
      };
    });

    const expectedWorkDays = expectedIndexes.length;
    const expectedWeeklyExactMinutes = expectedWorkDays > 0
      ? (weeklyTargetMinutes * expectedWorkDays) / baseWorkDays
      : 0;
    const expectedWeeklyRoundedMinutes = expectedWorkDays > 0
      ? roundMinutesToInterval(expectedWeeklyExactMinutes, programmingIntervalMinutes)
      : 0;
    const expectedWeeklyHours = minutesToHours(expectedWeeklyRoundedMinutes);
    const roundedTotalMinutes = Math.round(expectedWeeklyRoundedMinutes);
    const baseMinutes = expectedWorkDays > 0 ? Math.floor(roundedTotalMinutes / expectedWorkDays) : 0;
    const remainderMinutes = expectedWorkDays > 0 ? roundedTotalMinutes % expectedWorkDays : 0;
    const expectedHoursByIndex = new Map();
    expectedIndexes.forEach((index, position) => {
      const minutes = baseMinutes + (position < remainderMinutes ? 1 : 0);
      expectedHoursByIndex.set(index, minutesToHours(minutes));
    });

    return {
      mandatoryRestIndex,
      baseWorkDays,
      expectedWorkDays,
      expectedWeeklyHours,
      expectedWeeklyExactMinutes,
      roundingAdjustmentMinutes: Math.round(expectedWeeklyRoundedMinutes - expectedWeeklyExactMinutes),
      dayStates: provisionalStates.map((dayState) => ({
        ...dayState,
        expectedHours: expectedHoursByIndex.get(dayState.dayIndex) || 0,
      })),
    };
  };

  const isCompleteWorkDay = (dayState, dayReferenceValue) => {
    const workedHours = roundHours(dayState.dailyHours ?? dayState.workedHours ?? 0);
    if (workedHours <= 0.001) {
      return false;
    }
    if (dayState.expectedReason === "fuera_de_rango") {
      return false;
    }
    if (dayState.isLeaveDay || dayState.isNonWorkedHoliday) {
      return false;
    }
    const modeValue = dayState.modeValue ?? dayState.mode ?? "";
    if (modeValue === "pay_day" || advanceDayModes.has(modeValue)) {
      return false;
    }
    const expectedHours = roundHours(dayState.expectedHours || 0);
    const configuredDayHours = roundHours(dayState.dailyTargetHours || 0);
    let referenceHours = Math.max(expectedHours, roundHours(dayReferenceValue || 0));
    if (configuredDayHours > 0.001) {
      referenceHours = Math.min(referenceHours > 0.001 ? referenceHours : configuredDayHours, configuredDayHours);
    }
    if (referenceHours <= 0.001) {
      return false;
    }
    const referenceMinutes = hoursToMinutes(referenceHours);
    const wholeHours = Math.floor(referenceMinutes / 60);
    const fractionalMinutes = referenceMinutes - (wholeHours * 60);
    let thresholdMinutes = wholeHours * 60;
    if (fractionalMinutes === 30) {
      thresholdMinutes = referenceMinutes;
    } else if (thresholdMinutes <= 0) {
      thresholdMinutes = referenceMinutes;
    }
    const threshold = minutesToHours(thresholdMinutes);
    return threshold > 0.001 && workedHours + 0.001 >= threshold;
  };

  const getCompanyDayRepaymentExclusionHours = (entry, dayReferenceValue) => {
    const workedHours = roundHours(entry.workedHours || 0);
    const expectedHours = roundHours(entry.expectedHours || 0);
    const configuredDayHours = roundHours(entry.dailyTargetHours || 0);
    const fullDayHours = Math.max(expectedHours, configuredDayHours, roundHours(dayReferenceValue || 0));
    if (fullDayHours <= 0.001) {
      return workedHours;
    }
    return roundHours(Math.min(workedHours, fullDayHours));
  };

  const getCompanyDayRepaymentAutoPriority = (entry) => {
    if (entry.isMandatoryRestDay) {
      return [0, entry.index];
    }
    if (entry.index === 0) {
      return [1, entry.index];
    }
    if (entry.specialGenerated || entry.isHoliday) {
      return [2, entry.index];
    }
    return [3, entry.index];
  };

  const getCompanyDayRepaymentErrorMessage = (reason) => {
    if (reason === "no_negative_balance") {
      return "Solo puedes compensar un dia a favor de la empresa si el trabajador tiene saldo negativo previo.";
    }
    if (reason === "partial_day") {
      return "La jornada no puede utilizarse para compensar el descanso adelantado porque no corresponde a un dia completo.";
    }
    if (reason === "no_additional_full_day") {
      return "La compensacion requiere un dia completo adicional respecto de los dias base del cargo.";
    }
    return "No fue posible aplicar la compensacion del dia a favor de la empresa.";
  };

  const buildCompanyDayRepaymentPlan = (entries, startingDayBalance, dayReferenceValue, baseWorkDaysValue) => {
    const pendingCompanyDays = Math.floor(Math.abs(Math.min(roundHours(startingDayBalance), 0)));
    const fullWorkDayIndexes = entries
      .filter((entry) => isCompleteWorkDay(entry, dayReferenceValue))
      .map((entry) => entry.index);
    const additionalFullWorkDays = Math.max(fullWorkDayIndexes.length - Math.max(Number(baseWorkDaysValue || 0), 0), 0);
    const repaymentCapacity = Math.min(additionalFullWorkDays, pendingCompanyDays);
    const markedRepaymentIndexes = entries
      .filter((entry) => entry.mode === companyDayRepaymentMode)
      .map((entry) => entry.index);
    const hasManualSelection = markedRepaymentIndexes.length > 0;
    const invalidRepaymentReasons = {};
    const validRepaymentIndexes = [];
    const automaticRepaymentIndexes = [];
    const excludedHoursByIndex = {};
    const candidateEntries = [];

    entries.forEach((entry) => {
      const isManualSelection = entry.mode === companyDayRepaymentMode;
      if (hasManualSelection && !isManualSelection) {
        return;
      }
      if (pendingCompanyDays <= 0) {
        if (isManualSelection) {
          invalidRepaymentReasons[entry.index] = "no_negative_balance";
        }
        return;
      }
      if (!isCompleteWorkDay(entry, dayReferenceValue)) {
        if (isManualSelection) {
          invalidRepaymentReasons[entry.index] = "partial_day";
        }
        return;
      }
      candidateEntries.push(entry);
    });

    const selectedEntries = hasManualSelection
      ? [...candidateEntries].sort((left, right) => left.index - right.index)
      : [...candidateEntries].sort((left, right) => {
        const [leftRank, leftIndex] = getCompanyDayRepaymentAutoPriority(left);
        const [rightRank, rightIndex] = getCompanyDayRepaymentAutoPriority(right);
        return leftRank - rightRank || leftIndex - rightIndex;
      });

    selectedEntries.forEach((entry, position) => {
      const index = entry.index;
      if (position >= repaymentCapacity) {
        if (hasManualSelection) {
          invalidRepaymentReasons[index] = "no_additional_full_day";
        }
        return;
      }
      validRepaymentIndexes.push(index);
      if (!hasManualSelection) {
        automaticRepaymentIndexes.push(index);
      }
      excludedHoursByIndex[index] = getCompanyDayRepaymentExclusionHours(entry, dayReferenceValue);
    });

    return {
      startingDayBalance: roundHours(startingDayBalance),
      pendingCompanyDays,
      fullWorkDayIndexes,
      completeWorkDays: fullWorkDayIndexes.length,
      additionalFullWorkDays,
      repaymentCapacity,
      markedRepaymentIndexes,
      validRepaymentIndexes,
      automaticRepaymentIndexes,
      invalidRepaymentReasons,
      excludedHoursByIndex,
    };
  };

  const getShiftMetrics = (label) => {
    const normalized = String(label || "").trim();
    if (!normalized) {
      return { hours: 0, night_hours: 0 };
    }

    if (Object.prototype.hasOwnProperty.call(shiftMetrics, normalized)) {
      return {
        hours: parseDecimal(shiftMetrics[normalized].hours),
        night_hours: parseDecimal(shiftMetrics[normalized].night_hours),
      };
    }

    return parseRangeMetrics(normalized);
  };

  const resolvePaymentUsage = (
    entries,
    availableDayBalance,
    startingDayBalance,
    availableHourBalance,
    weeklyTargetHoursValue,
    baseWorkDaysValue,
    dayReferenceValue,
  ) => {
    let remainingDayBalance = roundHours(availableDayBalance);
    let remainingHourBalance = clampNonNegative(availableHourBalance);
    let cumulativeCountedHours = 0;
    let cumulativeOvertimeHours = 0;
    const repaymentPlan = buildCompanyDayRepaymentPlan(
      entries,
      startingDayBalance,
      dayReferenceValue,
      baseWorkDaysValue,
    );
    const validRepaymentIndexes = new Set(repaymentPlan.validRepaymentIndexes);
    const automaticRepaymentIndexes = new Set(repaymentPlan.automaticRepaymentIndexes);

    let paymentDaysUsed = 0;
    let advanceRestDaysUsed = 0;
    let additionalRestDaysUsed = 0;
    let companyDayRepaymentsUsed = 0;
    let moneyPaymentDaysUsed = 0;
    let paymentHoursUsed = 0;
    let moneyPaymentHoursUsed = 0;
    const invalidPayDayIndices = [];
    const invalidPayMoneyDayIndices = [];
    const invalidPayHoursIndices = [];
    const invalidPayMoneyIndices = [];
    const invalidCompanyDayRepaymentIndices = [];
    const invalidAdvanceDayIndices = [];
    const invalidAutoRestDayIndices = [];
    let generatedSpecialDays = 0;
    let excludedCompanyDayHours = 0;
    const dayStates = {};

    [...entries]
      .sort((left, right) => left.index - right.index)
      .forEach((entry) => {
        const requestedHours = roundHours(entry.hours);
        const workedHours = roundHours(entry.workedHours);
        const specialGenerated = Boolean(entry.specialGenerated);
        const dayState = {
          mode: entry.mode,
          requestedHours,
          source: "",
          valid: true,
          availableDayBalance: remainingDayBalance,
          availableHourBalance: remainingHourBalance,
          availableAdvancePendingBalance: 0,
          remainingDayBalance,
          remainingHourBalance,
          remainingAdvancePendingBalance: 0,
          generatedDay: false,
          generatedHours: 0,
          hourDifference: 0,
          appliedDayDelta: 0,
          repaymentReason: "",
          isCompleteWorkDay: isCompleteWorkDay(entry, dayReferenceValue),
          excludedHoursFromOvertime: 0,
        };

        let repaysCompanyDay = false;
        const selectedAutomaticRepayment =
          entry.mode !== companyDayRepaymentMode && automaticRepaymentIndexes.has(entry.index);
        if (entry.mode === companyDayRepaymentMode || selectedAutomaticRepayment) {
          if (validRepaymentIndexes.has(entry.index)) {
            remainingDayBalance = roundHours(remainingDayBalance + 1);
            companyDayRepaymentsUsed += 1;
            repaysCompanyDay = true;
            dayState.source = entry.mode === companyDayRepaymentMode ? "signed_day_balance" : "automatic_repayment";
            dayState.appliedDayDelta = 1;
          } else {
            invalidCompanyDayRepaymentIndices.push(entry.index);
            dayState.source = "repayment_invalid";
            dayState.valid = false;
            dayState.repaymentReason = repaymentPlan.invalidRepaymentReasons[entry.index] || "";
          }
        } else if (entry.mode === "pay_day") {
          const projectedBalance = roundHours(remainingDayBalance - 1);
          if (projectedBalance >= -2 - 0.001) {
            remainingDayBalance = projectedBalance;
            paymentDaysUsed += 1;
            dayState.source = "signed_day_balance";
            dayState.appliedDayDelta = -1;
          } else {
            invalidPayDayIndices.push(entry.index);
            dayState.source = "day_limit";
            dayState.valid = false;
          }
        } else if (moneyDayModes.has(entry.mode)) {
          if (remainingDayBalance + 0.001 >= 1) {
            remainingDayBalance = roundHours(remainingDayBalance - 1);
            moneyPaymentDaysUsed += 1;
            dayState.source = "day_balance";
            dayState.appliedDayDelta = -1;
          } else {
            invalidPayMoneyDayIndices.push(entry.index);
            dayState.source = "insufficient";
            dayState.valid = false;
          }
        } else if (advanceDayModes.has(entry.mode)) {
          const projectedBalance = roundHours(remainingDayBalance - 1);
          if (projectedBalance >= -2 - 0.001) {
            remainingDayBalance = projectedBalance;
            advanceRestDaysUsed += 1;
            additionalRestDaysUsed += 1;
            dayState.source = "signed_day_balance";
            dayState.appliedDayDelta = -1;
          } else {
            invalidAdvanceDayIndices.push(entry.index);
            dayState.source = "day_limit";
            dayState.valid = false;
          }
        } else if (entry.isAdditionalRestDay) {
          const projectedBalance = roundHours(remainingDayBalance - 1);
          if (projectedBalance >= -2 - 0.001) {
            remainingDayBalance = projectedBalance;
            additionalRestDaysUsed += 1;
            dayState.source = "signed_day_balance";
            dayState.appliedDayDelta = -1;
          } else {
            invalidAutoRestDayIndices.push(entry.index);
            dayState.source = "day_limit";
            dayState.valid = false;
          }
        } else if (entry.mode === "pay_hours") {
          if (requestedHours <= 0.001 || remainingHourBalance + 0.001 < requestedHours) {
            invalidPayHoursIndices.push(entry.index);
            dayState.valid = false;
            dayState.source = "insufficient";
          } else {
            paymentHoursUsed = roundHours(paymentHoursUsed + requestedHours);
            remainingHourBalance = clampNonNegative(remainingHourBalance - requestedHours);
            dayState.source = "hour_balance";
          }
        } else if (moneyHourModes.has(entry.mode)) {
          if (requestedHours <= 0.001 || remainingHourBalance + 0.001 < requestedHours) {
            invalidPayMoneyIndices.push(entry.index);
            dayState.valid = false;
            dayState.source = "insufficient";
          } else {
            moneyPaymentHoursUsed = roundHours(moneyPaymentHoursUsed + requestedHours);
            remainingHourBalance = clampNonNegative(remainingHourBalance - requestedHours);
            dayState.source = "hour_balance";
          }
        }

        if (
          specialGenerated
          && dayState.isCompleteWorkDay
          && workedHours > 0.001
          && entry.mode !== companyDayRepaymentMode
          && !selectedAutomaticRepayment
        ) {
          remainingDayBalance = roundHours(remainingDayBalance + 1);
          dayState.generatedDay = true;
          generatedSpecialDays += 1;
        }

        const excludedHours = repaysCompanyDay ? roundHours(repaymentPlan.excludedHoursByIndex[entry.index] || 0) : 0;
        if (excludedHours > 0.001) {
          excludedCompanyDayHours = roundHours(excludedCompanyDayHours + excludedHours);
          dayState.excludedHoursFromOvertime = excludedHours;
        }
        const previousOvertimeHours = cumulativeOvertimeHours;
        cumulativeCountedHours = roundHours(cumulativeCountedHours + Math.max(workedHours - excludedHours, 0));
        cumulativeOvertimeHours = roundHours(Math.max(cumulativeCountedHours - weeklyTargetHoursValue, 0));
        const generatedHours = roundHours(Math.max(cumulativeOvertimeHours - previousOvertimeHours, 0));
        if (generatedHours > 0.001) {
          remainingHourBalance = roundHours(remainingHourBalance + generatedHours);
          dayState.generatedHours = generatedHours;
        }
        dayState.hourDifference = generatedHours;

        dayState.remainingDayBalance = remainingDayBalance;
        dayState.remainingHourBalance = remainingHourBalance;
        dayStates[entry.index] = dayState;
      });

    return {
      paymentDaysUsed,
      advanceRestDaysUsed,
      additionalRestDaysUsed,
      companyDayRepaymentsUsed,
      automaticCompanyDayRepaymentsUsed: automaticRepaymentIndexes.size,
      automaticRepaymentIndexes: [...automaticRepaymentIndexes].sort((left, right) => left - right),
      paymentDaysFromDayBalance: paymentDaysUsed,
      paymentDaysFromHourBalance: 0,
      uncoveredPaymentDays: invalidPayDayIndices.length,
      moneyPaymentDaysUsed,
      paymentDayHourEquivalent: 0,
      paymentHoursUsed,
      moneyPaymentHoursUsed,
      invalidPayDayIndices,
      invalidPayMoneyDayIndices,
      invalidPayHoursIndices,
      invalidPayMoneyIndices,
      invalidCompanyDayRepaymentIndices,
      invalidCompanyDayRepaymentReasons: repaymentPlan.invalidRepaymentReasons,
      invalidAdvanceDayIndices,
      invalidAdvanceDayWithBalanceIndices: [],
      invalidAutoRestDayIndices,
      generatedSpecialDays,
      excludedCompanyDayHours,
      generatedOvertimeHours: cumulativeOvertimeHours,
      remainingDayBalance,
      remainingHourBalance,
      remainingAdvancePendingBalance: 0,
      repaymentPlan,
      dayStates,
    };
  };

  const updateCompensationControl = (modeSelect, hoursInput) => {
    if (!modeSelect) {
      return;
    }
    const paymentBlock = modeSelect.closest(".day-payment");
    const hoursWrap = paymentBlock?.querySelector("[data-pay-hours-wrap]");
    const needsHours = compensationModesWithHours.has(modeSelect.value);
    if (hoursWrap) {
      hoursWrap.hidden = !needsHours;
    }
    if (hoursInput) {
      hoursInput.required = needsHours;
      if (!needsHours) {
        hoursInput.value = "";
      }
    }
  };

  const rows = document.querySelectorAll(".schedule-row");

  const parseExternalLoanHoursByIndex = (rawValue) => {
    try {
      return JSON.parse(rawValue || "{}") || {};
    } catch (_error) {
      return {};
    }
  };

  const markAutosaveDirty = () => {
    if (!autosaveEnabled || scheduleClosed) {
      return;
    }
    autosaveDirty = true;
    lastInteractionAt = Date.now();
  };

  const autosaveScheduleForm = async () => {
    if (!autosaveEnabled || scheduleClosed || manualSubmitInProgress || autosaveInFlight || !autosaveDirty) {
      return;
    }
    if (Date.now() - lastInteractionAt < 1500) {
      return;
    }

    autosaveInFlight = true;
    try {
      const formData = new FormData(scheduleForm);
      const response = await fetch(scheduleForm.action || window.location.href, {
        method: "POST",
        headers: {
          "X-Requested-With": "XMLHttpRequest",
          "X-Schedule-Autosave": "true",
          "X-CSRFToken": csrfToken,
          Accept: "application/json",
        },
        body: formData,
        credentials: "same-origin",
      });

      if (!response.ok) {
        return;
      }

      const payload = await response.json();
      if (payload?.ok) {
        autosaveDirty = false;
      }
    } catch (_error) {
      // El autoguardado es silencioso: si falla, se reintentara en el siguiente ciclo.
    } finally {
      autosaveInFlight = false;
    }
  };

  scheduleForm.addEventListener(
    "input",
    () => {
      markAutosaveDirty();
    },
    true,
  );
  scheduleForm.addEventListener(
    "change",
    () => {
      markAutosaveDirty();
    },
    true,
  );
  scheduleForm.addEventListener("submit", () => {
    manualSubmitInProgress = true;
  });

  if (autosaveEnabled && !scheduleClosed) {
    window.setInterval(() => {
      autosaveScheduleForm();
    }, 30000);
  }

  rows.forEach((row) => {
    const weeklyTarget = parseDecimal(row.dataset.weeklyTarget);
    const dailyMax = parseDecimal(row.dataset.dailyMax);
    const baseWorkDays = Number.parseInt(row.dataset.baseWorkDays || `${defaultBaseWorkDays}`, 10) || defaultBaseWorkDays;
    const scopeIndexes = new Set(
      String(row.dataset.scopeIndexes || "0,1,2,3,4,5,6")
        .split(",")
        .map((value) => Number.parseInt(value, 10))
        .filter((value) => Number.isInteger(value)),
    );
    const externalLoanHoursByIndex = parseExternalLoanHoursByIndex(row.dataset.externalLoanHours);
    const priorDayBalance = roundHours(parseDecimal(row.dataset.priorDayBalance));
    const priorHourBalance = clampNonNegative(parseDecimal(row.dataset.priorHourBalance));
    const dayReferenceHours = parseDecimal(row.dataset.dayReferenceHours);
    const hasOvertimeRestriction = row.dataset.overtimeRestrictionActive === "true";
    const overtimeRestrictionDailyLimit = parseDecimal(row.dataset.overtimeRestrictionDailyLimit);
    const overtimeRestrictionWeeklyLimit = parseDecimal(row.dataset.overtimeRestrictionWeeklyLimit);
    const effectiveWeeklyTarget = weeklyTarget > 0 ? weeklyTarget : defaultWeeklyHours;
    const effectiveDailyMax = dailyMax > 0 ? dailyMax : defaultDailyMax;
    const totalCell = row.querySelector("[data-total-hours]");
    const overtimeCell = row.querySelector("[data-overtime-hours]");
    const nightCell = row.querySelector("[data-night-hours]");
    const dayBalanceCell = row.querySelector("[data-day-balance]");
    const hourBalanceCell = row.querySelector("[data-hour-balance]");
    const summaryCell = row.querySelector("[data-live-summary]");
    const balanceNote = row.querySelector("[data-balance-note]");
    const alertsCell = row.querySelector(".alerts-cell");
    const persistedSummaries = row.querySelectorAll("[data-persisted-summary]");
    const initialDayBalanceText = dayBalanceCell?.textContent || "";
    const initialHourBalanceText = hourBalanceCell?.textContent || "";
    const initialSummaryText = summaryCell?.textContent || "";
    const initialSummaryHidden = summaryCell?.hidden ?? true;
    const initialBalanceNoteText = balanceNote?.textContent || "";
    const manualDayAdjustmentInput = row.querySelector('input[name$="-manual_day_adjustment"]');
    const manualHourAdjustmentInput = row.querySelector('input[name$="-manual_hour_adjustment"]');
    const dayCells = row.querySelectorAll("[data-day-index]");
    const employeeName = row.querySelector(".schedule-cell-employee")?.textContent?.trim() || "Esta persona";
    const rowHasServerErrors = Boolean(row.querySelector(".row-errors, .errorlist"));

    const confirmInventoryParticipation = (event) => {
      const checkbox = event.currentTarget;
      if (!checkbox.checked) {
        return;
      }
      const dayCell = checkbox.closest("[data-day-index]");
      const dayLabel = dayCell?.dataset.dayLabel || "este dia";
      const confirmed = window.confirm(
        `Esta seguro que ${employeeName} participa en el inventario del ${dayLabel}?`,
      );
      if (!confirmed) {
        checkbox.checked = false;
      }
    };

    const updatePaymentInfo = (dayCell, modeValue, state) => {
      const paymentInfo = dayCell.querySelector("[data-payment-info]");
      if (!paymentInfo) {
        return;
      }

      if (modeValue === "pay_day") {
        paymentInfo.hidden = false;
        if (state.paymentState?.source === "signed_day_balance") {
          paymentInfo.textContent = `Pago dia: descuenta 1 dia del saldo neto. Resultado estimado: ${describeDayBalance(state.paymentState.remainingDayBalance)}.`;
        } else {
          paymentInfo.textContent = "Pago dia: no puede dejar a la persona con mas de 2 dias a favor de la empresa.";
        }
        return;
      }

      if (moneyDayModes.has(modeValue)) {
        paymentInfo.hidden = false;
        if (state.paymentState?.source === "day_balance") {
          paymentInfo.textContent = `Pago en dinero por dia: descuenta 1 dia acumulado. Saldo estimado tras este pago: ${formatBalanceHours(state.paymentState.remainingDayBalance)} dia(s).`;
        } else {
          paymentInfo.textContent = "Pago en dinero por dia: requiere 1 dia acumulado disponible.";
        }
        return;
      }

      if (advanceDayModes.has(modeValue)) {
        paymentInfo.hidden = false;
        if (state.paymentState?.source === "signed_day_balance") {
          paymentInfo.textContent = `Descanso adelantado: descuenta 1 dia del saldo neto. Resultado estimado: ${describeDayBalance(state.paymentState.remainingDayBalance)}.`;
        } else {
          paymentInfo.textContent = "Descanso adelantado: no puede dejar a la persona con mas de 2 dias a favor de la empresa.";
        }
        return;
      }

      if (modeValue === companyDayRepaymentMode) {
        paymentInfo.hidden = false;
        if (state.paymentState?.source === "signed_day_balance") {
          paymentInfo.textContent = `Compensacion empresa: esta jornada completa aplicara 1 dia a favor de la empresa y no generara un nuevo dia por esta misma fecha.`;
        } else {
          paymentInfo.textContent = getCompanyDayRepaymentErrorMessage(state.paymentState?.repaymentReason || "");
        }
        return;
      }

      if (state.paymentState?.source === "automatic_repayment") {
        paymentInfo.hidden = false;
        paymentInfo.textContent = "Compensacion automatica: esta jornada completa aplicara 1 dia a favor de la empresa y no generara un nuevo dia por esta misma fecha.";
        return;
      }

      if (modeValue === "pay_hours") {
        paymentInfo.hidden = false;
        const coveredHours = state.dailyHours + state.compensationHoursValue;
        const targetDailyHours = state.targetDailyHours || 0;
        if (coveredHours >= targetDailyHours - 0.001) {
          paymentInfo.textContent = `Trabajadas ${formatHours(state.dailyHours)} h + pagas ${formatHours(state.compensationHoursValue)} h = jornada cubierta.`;
        } else {
          paymentInfo.textContent = `Trabajadas ${formatHours(state.dailyHours)} h + pagas ${formatHours(state.compensationHoursValue)} h. Faltan ${formatHours(targetDailyHours - coveredHours)} h.`;
        }
        return;
      }

      if (moneyHourModes.has(modeValue)) {
        paymentInfo.hidden = false;
        paymentInfo.textContent = `Pago en dinero por horas: descuenta ${formatHours(state.compensationHoursValue)} h del saldo acumulado. Saldo estimado tras este pago: ${formatBalanceHours(state.paymentState?.remainingHourBalance ?? state.endingHourBalance)} h.`;
        return;
      }

      paymentInfo.hidden = true;
      paymentInfo.textContent = "";
    };

    const updateBalanceNote = (endingDayBalance, endingHourBalance) => {
      if (!balanceNote) {
        return;
      }
      balanceNote.textContent = `Saldo previo: ${describeDayBalance(priorDayBalance)} y ${formatBalanceHours(priorHourBalance)} h. Resultado estimado: ${describeDayBalance(endingDayBalance)} y ${formatBalanceHours(endingHourBalance)} h.`;
    };

  const buildLiveSummary = (summaryState) => {
      const statusBlocksTransition = Boolean(summaryState.blocksStatusTransition);
      const statusLabel = getDisplayValidationStatus(
        summaryState.validationStatus,
        summaryState.weeklyHourDifference,
      );
      const liveMessages = [];
      if (statusBlocksTransition) {
        liveMessages.push(getStatusBlockerMessage(summaryState.validationStatus, summaryState.weeklyHourDifference));
      }
      liveMessages.push(`Resultado estimado: ${describeDayBalance(summaryState.endingDayBalance)} y ${describeWeeklyDifference(summaryState.weeklyHourDifference)}.`);
      if (summaryState.generatedSundayDays > 0 || summaryState.generatedHolidayDays > 0 || summaryState.paidDays > 0) {
        const movementMessages = [];
        if (summaryState.generatedSundayDays > 0) {
          movementMessages.push(`${summaryState.generatedSundayDays} dia(s) generado(s) por domingo trabajado`);
        }
        if (summaryState.generatedHolidayDays > 0) {
          movementMessages.push(`${summaryState.generatedHolidayDays} dia(s) generado(s) por festivo trabajado`);
        }
        if (summaryState.paidDays > 0) {
          movementMessages.push(`${summaryState.paidDays} dia(s) consumido(s) mediante Pago dia`);
        }
        liveMessages.push(`Movimientos: ${movementMessages.join("; ")}.`);
      }
      liveMessages.push(`Estado: ${statusLabel}.`);
      liveMessages.push(`Jornada ajustada: ${formatHours(summaryState.expectedWeeklyHours, true)}. Programadas: ${formatHours(summaryState.totalHours, true)}. Diferencia: ${formatHours(summaryState.weeklyHourDifference, true)}.`);
      if (summaryState.roundingAdjustmentMinutes !== 0) {
        liveMessages.push(`Calculo exacto previo al redondeo: ${formatMinutesDuration(summaryState.expectedWeeklyExactMinutes)}. Ajuste tecnico: ${formatSignedMinutesDuration(summaryState.roundingAdjustmentMinutes)}.`);
      }
      liveMessages.push(`Capacidad disponible: ${formatHours(summaryState.capacityHours, true)}. Dias base: ${summaryState.baseWorkDays}. Descansos adicionales: ${summaryState.additionalRestDays}.`);
      if (summaryState.specialDaysGenerated > 0.001) {
        liveMessages.push(`Genera ${formatHours(summaryState.specialDaysGenerated)} dia(s) por domingos/festivos.`);
      }
      if (summaryState.externalLoanHours > 0.001) {
        liveMessages.push(`Horas de prestamo en otra sede: ${formatHours(summaryState.externalLoanHours, true)}.`);
      }
      if (summaryState.unlinkedLoanDays > 0) {
        liveMessages.push(`Prestamo sin sede destino: reduce ${summaryState.unlinkedLoanDays} dia(s) de la jornada y no mueve saldos.`);
      }
      if (summaryState.companyDayRepaymentsUsed > 0) {
        liveMessages.push(`Compensa ${summaryState.companyDayRepaymentsUsed} dia(s) a favor de la empresa.`);
      }
      if ((summaryState.automaticCompanyDayRepaymentsUsed || 0) > 0) {
        liveMessages.push(`Compensacion automatica aplicada en ${summaryState.automaticCompanyDayRepaymentsUsed} dia(s).`);
      }
      if (summaryState.overtimeHours > 0.001) {
        liveMessages.push(`Extras calculadas: ${formatHours(summaryState.overtimeHours, true)}.`);
      }
      if (summaryState.excludedCompanyDayHours > 0.001) {
        liveMessages.push(`Horas excluidas por compensacion: ${formatHours(summaryState.excludedCompanyDayHours, true)}.`);
      }
      if (summaryState.overtimeDailyRestrictionExceededCount > 0) {
        liveMessages.push(
          `Restriccion medica diaria: ${summaryState.overtimeDailyRestrictionExceededCount} dia(s) supera(n) ${formatHours(summaryState.overtimeRestrictionDailyLimit, true)} extra(s).`,
        );
      }
      if (summaryState.overtimeWeeklyRestrictionExceeded) {
        liveMessages.push(
          `Restriccion medica semanal: no puede superar ${formatHours(summaryState.overtimeRestrictionWeeklyLimit, true)} extra(s) y esta semana lleva ${formatHours(summaryState.overtimeHours, true)}.`,
        );
      }
      if (showNightHours && summaryState.totalNightHours > 0.001) {
        liveMessages.push(`Recargo nocturno acumulado: ${formatHours(summaryState.totalNightHours, true)}.`);
      }
      if (summaryState.daysOverLimit > 0) {
        liveMessages.push(`${summaryState.daysOverLimit} dia(s) supera(n) el maximo diario.`);
      }
      if (summaryState.invalidPayDayCount > 0) {
        liveMessages.push("Hay descansos adicionales que superan el limite de 2 dias a favor de la empresa.");
      }
      if (summaryState.invalidPayMoneyDayCount > 0) {
        liveMessages.push("Hay pagos en dinero por dia sin un dia acumulado disponible.");
      }
      if (summaryState.invalidAdvanceDayCount > 0 || summaryState.invalidAutoRestDayCount > 0) {
        liveMessages.push("Hay descansos adicionales bloqueados por superar el limite de 2 dias a favor de la empresa.");
      }
      if (summaryState.invalidCompanyDayRepaymentCount > 0) {
        liveMessages.push("Hay compensaciones de dias a favor de la empresa que no cumplen la jornada completa o no tienen un dia adicional disponible.");
      }
      if (summaryState.invalidHourDiscountCount > 0) {
        liveMessages.push("Hay descuentos por horas que superan el saldo acumulado disponible.");
      }
      if (summaryState.payHoursOverTargetCount > 0) {
        liveMessages.push("Hay dias donde el pago horas supera la jornada.");
      }
      if (summaryState.payMoneyOverTargetCount > 0) {
        liveMessages.push("Hay pagos en dinero por horas que superan la jornada diaria permitida.");
      }
      if (summaryState.payHoursIncompleteCount > 0) {
        liveMessages.push("Hay dias con pago horas que aun no completan la jornada.");
      }
      if (summaryState.invalidPositiveHoursCount > 0) {
        liveMessages.push("Hay pagos o descuentos por horas con cantidad invalida.");
      }
      if (summaryState.manualDayAdjustment !== 0 || summaryState.manualHourAdjustment !== 0) {
        liveMessages.push(`Ajuste manual aplicado: ${formatHours(summaryState.manualDayAdjustment)} dia(s) y ${formatHours(summaryState.manualHourAdjustment)} h.`);
      }

      if (showDetailedAlerts) {
        return liveMessages;
      }

      const conciseMessages = [];
      if (statusBlocksTransition) {
        conciseMessages.push(getStatusBlockerMessage(summaryState.validationStatus, summaryState.weeklyHourDifference));
      } else {
        conciseMessages.push(`Estado: ${statusLabel}.`);
      }
      if (summaryState.specialDaysGenerated > 0.001) {
        conciseMessages.push(`Dia(s) generado(s): ${formatHours(summaryState.specialDaysGenerated)}.`);
      }
      if (summaryState.companyDayRepaymentsUsed > 0) {
        conciseMessages.push(`Compensaciones empresa: ${summaryState.companyDayRepaymentsUsed}.`);
      }
      if (summaryState.overtimeHours > 0.001) {
        conciseMessages.push(`Extras: ${formatHours(summaryState.overtimeHours, true)}.`);
      }
      if (summaryState.overtimeDailyRestrictionExceededCount > 0 || summaryState.overtimeWeeklyRestrictionExceeded) {
        conciseMessages.push("Revisa restriccion medica.");
      }
      if (
        summaryState.daysOverLimit > 0
        || summaryState.invalidPayDayCount > 0
        || summaryState.invalidAdvanceDayCount > 0
        || summaryState.invalidAutoRestDayCount > 0
        || summaryState.invalidCompanyDayRepaymentCount > 0
        || summaryState.invalidHourDiscountCount > 0
        || summaryState.payHoursOverTargetCount > 0
        || summaryState.payMoneyOverTargetCount > 0
        || summaryState.invalidPositiveHoursCount > 0
      ) {
        conciseMessages.push("Revisa limites o saldo.");
      }
      return conciseMessages;
    };

    const recalculateRow = ({ preservePersistedState = false } = {}) => {
      let totalHours = 0;
      let totalNightHours = 0;
      let daysOverLimit = 0;
      let specialDaysGenerated = 0;
      let payHoursOverTargetCount = 0;
      let payMoneyOverTargetCount = 0;
      let payHoursIncompleteCount = 0;
      let invalidPositiveHoursCount = 0;
      let overtimeDailyRestrictionExceededCount = 0;
      const rawDayStates = [];

      dayCells.forEach((dayCell) => {
        const dayIndex = Number.parseInt(dayCell.dataset.dayIndex || "0", 10);
        const shift1Select = row.querySelector(`[name$="-day_${dayIndex}_shift_1"]`);
        const shift2Select = row.querySelector(`[name$="-day_${dayIndex}_shift_2"]`);
        const compensationMode = row.querySelector(`[name$="-day_${dayIndex}_compensation_mode"]`);
        const compensationHours = row.querySelector(`[name$="-day_${dayIndex}_compensation_hours"]`);
        const modeValue = compensationMode?.value || "";

        if (!scheduleClosed && (modeValue === "pay_day" || advanceDayModes.has(modeValue))) {
          setSelectValue(shift1Select, "descanso");
          setSelectValue(shift2Select, "");
          if (compensationHours) {
            compensationHours.value = "";
          }
        }

        updateCompensationControl(compensationMode, compensationHours);

        const shift1 = shift1Select?.value || "";
        const shift2 = shift2Select?.value || "";
        const shift1Metrics = getShiftMetrics(shift1);
        const shift2Metrics = getShiftMetrics(shift2);
        const dailyHours = roundHours(shift1Metrics.hours + shift2Metrics.hours);
        const dailyNightHours = roundHours(shift1Metrics.night_hours + shift2Metrics.night_hours);
        const compensationHoursValue = roundHours(parseDecimal(compensationHours?.value));
        const specialDayLabel = dayCell.dataset.specialDay || "";
        const specialGenerated = Boolean(specialDayLabel) && dailyHours > 0.001;
        const externalLoanHours = roundHours(parseDecimal(externalLoanHoursByIndex[String(dayIndex)] || 0));
        const dailyOvertimeHours = roundHours(Math.max(dailyHours - dayReferenceHours, 0));
        const shiftCategories = new Set([
          getShiftNonWorkCategory(shift1),
          getShiftNonWorkCategory(shift2),
        ].filter(Boolean));

        totalHours = roundHours(totalHours + dailyHours);
        totalNightHours = roundHours(totalNightHours + dailyNightHours);

        if (specialGenerated) {
          specialDaysGenerated += 1;
        }

        if (moneyHourModes.has(modeValue)) {
          if (compensationHoursValue <= 0.001) {
            invalidPositiveHoursCount += 1;
          } else if (compensationHoursValue > effectiveDailyMax + 0.001) {
            payMoneyOverTargetCount += 1;
          }
        }

        const dayHours = dayCell.querySelector("[data-day-hours]");
        const dayNight = dayCell.querySelector("[data-day-night]");
        if (dayHours) {
          dayHours.textContent = formatHours(dailyHours, true);
          dayHours.classList.toggle("is-over-limit", effectiveDailyMax > 0 && dailyHours > effectiveDailyMax + 0.001);
        }

        if (dayNight && showNightHours) {
          if (dailyNightHours > 0.001) {
            dayNight.hidden = false;
            dayNight.textContent = `Rec. noct. ${formatHours(dailyNightHours, true)}`;
            dayNight.classList.add("has-night");
          } else {
            dayNight.hidden = true;
            dayNight.textContent = "";
            dayNight.classList.remove("has-night");
          }
        }

        const isOverLimit = effectiveDailyMax > 0 && dailyHours > effectiveDailyMax + 0.001;
        dayCell.classList.toggle("is-over-limit", isOverLimit);
        if (isOverLimit) {
          daysOverLimit += 1;
        }
        if (hasOvertimeRestriction && dailyOvertimeHours > overtimeRestrictionDailyLimit + 0.001) {
          overtimeDailyRestrictionExceededCount += 1;
        }
        rawDayStates.push({
          dayIndex,
          dayCell,
          modeValue,
          compensationHoursValue,
          dailyHours,
          externalLoanHours,
          dailyOvertimeHours,
          specialGenerated,
          specialDayLabel,
          shiftCategories,
        });
      });

      const expectedPlan = buildExpectedPlan(rawDayStates, effectiveWeeklyTarget, baseWorkDays, scopeIndexes);
      const dayStates = expectedPlan.dayStates;
      const externalLoanHours = roundHours(dayStates.reduce(
        (total, dayState) => total + roundHours(dayState.externalLoanHours || 0),
        0,
      ));
      dayStates.forEach((dayState) => {
        if (dayState.modeValue === "pay_hours") {
          const targetHours = dayState.expectedHours > 0.001 ? dayState.expectedHours : effectiveDailyMax;
          if (dayState.compensationHoursValue <= 0.001) {
            invalidPositiveHoursCount += 1;
          } else if (dayState.expectedHours <= 0.001) {
            payHoursOverTargetCount += 1;
          } else if (dayState.dailyHours + dayState.compensationHoursValue > effectiveDailyMax + 0.001) {
            payHoursOverTargetCount += 1;
          } else if (dayState.dailyHours + dayState.compensationHoursValue < targetHours - 0.001) {
            payHoursIncompleteCount += 1;
          }
        }
      });

      const manualDayAdjustment = roundHours(parseDecimal(manualDayAdjustmentInput?.value));
      const manualHourAdjustment = roundHours(parseDecimal(manualHourAdjustmentInput?.value));
      const paymentUsage = resolvePaymentUsage(
        dayStates.map((dayState) => ({
          index: dayState.dayIndex,
          mode: dayState.modeValue,
          hours: dayState.compensationHoursValue,
          workedHours: dayState.dailyHours,
          dailyTargetHours: effectiveDailyMax,
          expectedHours: dayState.expectedHours,
          expectedReason: dayState.expectedReason,
          specialGenerated: dayState.specialGenerated,
          isMandatoryRestDay: dayState.isMandatoryRestDay,
          isHoliday: dayState.isHoliday,
          isNonWorkedHoliday: dayState.isNonWorkedHoliday,
          isLeaveDay: dayState.isLeaveDay,
          isAdditionalRestDay: dayState.isAdditionalRestDay,
        })),
        priorDayBalance + manualDayAdjustment,
        priorDayBalance,
        priorHourBalance + manualHourAdjustment,
        effectiveWeeklyTarget,
        baseWorkDays,
        dayReferenceHours,
      );
      const creditedTotalHours = roundHours(totalHours + paymentUsage.paymentHoursUsed + externalLoanHours);
      const evaluatedTotalHours = roundHours(creditedTotalHours - paymentUsage.excludedCompanyDayHours);
      const weeklyHourDifference = roundHours(evaluatedTotalHours - expectedPlan.expectedWeeklyHours);
      const overtimeHours = roundHours(paymentUsage.generatedOvertimeHours);
      const overtimeWeeklyRestrictionExceeded =
        hasOvertimeRestriction && overtimeHours > overtimeRestrictionWeeklyLimit + 0.001;
      specialDaysGenerated = paymentUsage.generatedSpecialDays;
      const endingDayBalance = roundHours(paymentUsage.remainingDayBalance);
      const endingHourBalance = clampNonNegative(
        priorHourBalance
        + overtimeHours
        + manualHourAdjustment
        - paymentUsage.paymentHoursUsed
        - paymentUsage.moneyPaymentHoursUsed,
      );
      dayStates.forEach((dayState) => {
        updatePaymentInfo(dayState.dayCell, dayState.modeValue, {
          dailyHours: dayState.dailyHours,
          compensationHoursValue: dayState.compensationHoursValue,
          expectedHours: dayState.expectedHours,
          targetDailyHours: dayState.expectedHours > 0.001 ? dayState.expectedHours : effectiveDailyMax,
          endingDayBalance,
          endingHourBalance,
          paymentState: paymentUsage.dayStates[dayState.dayIndex],
        });
      });
      let generatedSundayDays = 0;
      let generatedHolidayDays = 0;
      let paidDays = 0;
      dayStates.forEach((dayState) => {
        const paymentState = paymentUsage.dayStates[dayState.dayIndex] || {};
        if (paymentState.generatedDay && dayState.specialDayLabel) {
          if (dayState.dayIndex === 0) {
            generatedSundayDays += 1;
          } else if (dayState.isHoliday) {
            generatedHolidayDays += 1;
          }
        }
        if (dayState.modeValue === "pay_day" && roundHours(paymentState.appliedDayDelta || 0) < -0.001) {
          paidDays += 1;
        }
      });

      if (totalCell) {
        totalCell.textContent = formatHours(creditedTotalHours);
      }
      if (overtimeCell) {
        overtimeCell.textContent = formatHours(overtimeHours);
      }
      if (nightCell) {
        nightCell.textContent = formatHours(totalNightHours);
      }
      if (dayBalanceCell) {
        if (preservePersistedState) {
          dayBalanceCell.textContent = initialDayBalanceText;
          dayBalanceCell.classList.toggle("metric-cell--negative", parseDecimal(initialDayBalanceText) < -0.001);
        } else {
          dayBalanceCell.textContent = formatBalanceHours(endingDayBalance);
          dayBalanceCell.classList.toggle("metric-cell--negative", endingDayBalance < -0.001);
        }
      }
      if (hourBalanceCell) {
        hourBalanceCell.textContent = preservePersistedState
          ? initialHourBalanceText
          : formatBalanceHours(endingHourBalance);
        hourBalanceCell.classList.remove("metric-cell--negative");
      }

      if (preservePersistedState) {
        if (balanceNote) {
          balanceNote.textContent = initialBalanceNoteText;
        }
      } else {
        updateBalanceNote(endingDayBalance, endingHourBalance);
      }

      const capacityHours = roundHours(expectedPlan.expectedWorkDays * effectiveDailyMax);
      const validationStatus =
        paymentUsage.invalidPayDayIndices.length > 0
        || (paymentUsage.invalidAdvanceDayIndices?.length || 0) > 0
        || (paymentUsage.invalidAutoRestDayIndices?.length || 0) > 0
        || paymentUsage.invalidCompanyDayRepaymentIndices.length > 0
          ? "INCONSISTENTE"
          : weeklyHourDifference > 0.001
            ? "EXCESO_PROGRAMADO"
            : weeklyHourDifference < -0.001
              ? (capacityHours + 0.001 >= expectedPlan.expectedWeeklyHours
                ? "INCOMPLETA_CORREGIBLE"
                : "IMPOSIBLE_POR_CAPACIDAD")
              : "VALIDA";
      const statusBlocksTransition = doesStatusBlockTransition(validationStatus, weeklyHourDifference);

      const liveMessages = buildLiveSummary({
        totalHours: creditedTotalHours,
        expectedWeeklyHours: expectedPlan.expectedWeeklyHours,
        expectedWeeklyExactMinutes: expectedPlan.expectedWeeklyExactMinutes,
        roundingAdjustmentMinutes: expectedPlan.roundingAdjustmentMinutes,
        weeklyHourDifference,
        capacityHours,
        baseWorkDays,
        additionalRestDays: expectedPlan.dayStates.filter((dayState) => (
          dayState.expectedReason === "descanso_compensatorio"
          || dayState.expectedReason === "descanso_adelantado"
          || dayState.expectedReason === "descanso_adicional"
        )).length,
        unlinkedLoanDays: expectedPlan.dayStates.filter(
          (dayState) => dayState.expectedReason === "prestamo_sin_destino",
        ).length,
        externalLoanHours,
        validationStatus,
        blocksStatusTransition: statusBlocksTransition,
        totalNightHours,
        overtimeHours,
        overtimeDailyRestrictionExceededCount,
        overtimeRestrictionDailyLimit,
        overtimeWeeklyRestrictionExceeded,
        overtimeRestrictionWeeklyLimit,
        daysOverLimit,
        specialDaysGenerated,
        companyDayRepaymentsUsed: paymentUsage.companyDayRepaymentsUsed,
        automaticCompanyDayRepaymentsUsed: paymentUsage.automaticCompanyDayRepaymentsUsed,
        excludedCompanyDayHours: paymentUsage.excludedCompanyDayHours,
        invalidPayDayCount: paymentUsage.invalidPayDayIndices.length,
        invalidPayMoneyDayCount: paymentUsage.invalidPayMoneyDayIndices.length,
        invalidAdvanceDayCount: paymentUsage.invalidAdvanceDayIndices?.length || 0,
        invalidAutoRestDayCount: paymentUsage.invalidAutoRestDayIndices?.length || 0,
        invalidCompanyDayRepaymentCount: paymentUsage.invalidCompanyDayRepaymentIndices.length,
        invalidHourDiscountCount: paymentUsage.invalidPayHoursIndices.length + paymentUsage.invalidPayMoneyIndices.length,
        payHoursOverTargetCount,
        payMoneyOverTargetCount,
        payHoursIncompleteCount,
        invalidPositiveHoursCount,
        manualDayAdjustment,
        manualHourAdjustment,
        endingDayBalance,
        endingHourBalance,
        generatedSundayDays,
        generatedHolidayDays,
        paidDays,
      });

      if (summaryCell) {
        if (preservePersistedState) {
          summaryCell.textContent = initialSummaryText;
          summaryCell.hidden = initialSummaryHidden;
        } else {
          summaryCell.textContent = liveMessages.join(" ");
          summaryCell.hidden = liveMessages.length === 0;
        }
        if (!preservePersistedState) {
          summaryCell.classList.toggle("live-summary--blocking", statusBlocksTransition);
        }
      }
      persistedSummaries.forEach((persistedSummary) => {
        persistedSummary.hidden = !preservePersistedState;
      });
      if (!preservePersistedState) {
        row.classList.toggle("schedule-row--blocking-status", statusBlocksTransition);
        alertsCell?.classList.toggle("alerts-cell--blocking", statusBlocksTransition);
        persistedSummaries.forEach((persistedSummary) => {
          persistedSummary.classList.toggle("persisted-summary--blocking", statusBlocksTransition);
        });
      }
    };

    const recalculateDirtyRow = () => recalculateRow({ preservePersistedState: false });

    if (!scheduleClosed) {
      row.querySelectorAll("select").forEach((field) => {
        field.addEventListener("change", (event) => {
          const changedField = event.currentTarget;
          if (changedField.name.includes("_shift_")) {
            const dayCell = changedField.closest("[data-day-index]");
            const dayIndex = dayCell?.dataset.dayIndex;
            const shift1Select = dayIndex === undefined
              ? null
              : row.querySelector(`[name$="-day_${dayIndex}_shift_1"]`);
            const shift2Select = dayIndex === undefined
              ? null
              : row.querySelector(`[name$="-day_${dayIndex}_shift_2"]`);
            const compensationMode = dayIndex === undefined
              ? null
              : row.querySelector(`[name$="-day_${dayIndex}_compensation_mode"]`);
            const compensationHours = dayIndex === undefined
              ? null
              : row.querySelector(`[name$="-day_${dayIndex}_compensation_hours"]`);
            const workedHours = roundHours(
              getShiftMetrics(shift1Select?.value || "").hours
              + getShiftMetrics(shift2Select?.value || "").hours,
            );

            if (
              workedHours > 0.001
              && (compensationMode?.value === "pay_day" || advanceDayModes.has(compensationMode?.value))
            ) {
              compensationMode.value = "";
              if (compensationHours) {
                compensationHours.value = "";
              }
            }
          }
          recalculateDirtyRow();
        });
      });

      row.querySelectorAll('input[name*="_compensation_hours"]').forEach((field) => {
        field.addEventListener("input", recalculateDirtyRow);
        field.addEventListener("change", recalculateDirtyRow);
      });

      [manualDayAdjustmentInput, manualHourAdjustmentInput].forEach((field) => {
        field?.addEventListener("input", recalculateDirtyRow);
        field?.addEventListener("change", recalculateDirtyRow);
      });

      row.querySelectorAll('[data-inventory-checkbox="true"]').forEach((field) => {
        field.addEventListener("change", confirmInventoryParticipation);
      });
    }

    recalculateRow({ preservePersistedState: scheduleClosed && !rowHasServerErrors });
  });

  const roleFilter = document.querySelector("[data-role-filter]");
  if (roleFilter) {
    const applyRoleFilter = () => {
      const selectedRole = String(roleFilter.value || "").trim().toLowerCase();
      rows.forEach((row) => {
        const rowRole = String(row.dataset.roleName || "").trim().toLowerCase();
        row.hidden = Boolean(selectedRole) && rowRole !== selectedRole;
      });
    };

    roleFilter.addEventListener("change", applyRoleFilter);
    scheduleForm.addEventListener("submit", () => {
      roleFilter.value = "";
      applyRoleFilter();
    });
    applyRoleFilter();
  }
}
