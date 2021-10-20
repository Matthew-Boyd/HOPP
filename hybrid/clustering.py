"""
Functions for clustering weather data and electricity pricing, and calculations of full-year or Cluster-average values
"""

import numpy as np
import pysolar
import datetime


class Clustering:

    def __init__(self, power_sources, solar_resource_file, wind_resource_data = None, price_data =None):
        
        self.power_sources = power_sources      # List of technologies included in the simulation ('pv', 'wind', 'tower', 'trough', 'battery', 'geothermal') 
        
        # Weather, price
        self.solar_resource_file = solar_resource_file  # Solar resource file (including wind speed that will be used for simulation of solar technologies)
        self.wind_resource = wind_resource_data         # Wind resource data (wind speed used for wind technologies)
        self.price = price_data           # Array of electricity prices (must match time resolution of solar resource file)
        self.price_limit = 3.5            # Limit for prices before clustering.  Values will be scaled if above (75th percentile + price_limit x interquartile range)
        self.wind_stow_limits = {'tower': 15, 'trough': 25, 'pv':None, 'wind':None}  # Wind stow speed (m/s) #TODO: Better to pull this from individual tech model inputs

        # Weights/divisions for calculation of classification metrics
        self.ndays = 2  # Number of simulation days in each group
        self.use_default_weights = True  # Use stored default values
        self.weights = {}        # User-specified weighting factors
        self.divisions = {}      # User-specified integer number of divisions per day
        self.bounds = {}         # Bounds ('fullday' or 'summer_daylight') to use for averaging.  Currently only defined default values are possible and will be filled in automatically

        # Clustering parameters
        self.algorithm = 'affinity-propagation'  # Clustering algorithm     
        self.n_cluster = 20                      # Number of clusters
        self.Nmaxiter = 200                      # Maximum iterations for clustering algorithm
        self.sim_hard_partitions = True          # Use hard partitioning for simulation weighting factors?

        self.afp_preference_mult = 1.0          # Multiplier for default preference values (median of input similarities = negative Euclidean distance b/w points i and k) --> Larger multiplier = fewer clusters
        self.afp_damping = 0.5                  # Damping factor in affinity propagation algorithm
        self.afp_Nconverge = 10                 # Number of iterations without change in solution for convergence
        self.afp_enforce_Ncluster = True        # Iterate on afp_preference_mult to create the number of clusters specified in n_cluster?
        self.afp_enforce_Ncluster_tol = 0       # Tolerance for number of clusters
        self.afp_enforce_Ncluster_maxiter = 50  # Maximum number of iterations

        # Results
        self.data = {}             # Classification data for complete groups (calculated in calculate_metrics())
        self.data_first = {}       # Classification data for incomplete group at the beginning of the year (calculated in calculate_metrics())
        self.data_last = {}        # Classification data for incomplete group at the end of the year (calculated in calculate_metrics())
        self.clusters = {}         # Clusters (calculated in create_clusters())
        self.sim_start_days = []   # Day of year at first day for each exemplar group (note this is the first "counted" day, not the preceeding day that must be simulated but not counted)
        self.index_first = -1      # Cluster index that best represents incomplete first group
        self.index_last = -1       # Cluster index that best represents incomplete last group
        self.daily_avg_dni = []    # Daily average DNI (used only for CSP initial charge state heuristic)



    def get_default_weights(self):
        iscsp = 'tower' in self.power_sources or 'trough' in self.power_sources
        ispv = 'pv' in self.power_sources
        iswind = 'wind' in self.power_sources
        isdispatch = iscsp or 'battery' in self.power_sources

        #TODO Add clearsky - actual DNI?    
        weights = {'dni':1.0 if iscsp else 0.0,
                   'dni_prev':0.5 if iscsp else 0.0,
                   'dni_next':0.5 if iscsp else 0.0,
                   'ghi':1.0 if ispv else 0.0,
                   'ghi_prev':0.5 if ispv and isdispatch else 0.0,
                   'ghi_next':0.5 if ispv and isdispatch else 0.0,
                   'tdry':0.25 if iscsp else 0.0,
                   'wspd_solar':0.0,    # Wind speed used for simulation of solar technologies (from wind data in solar_resource_file)
                   'wspd': 1.0 if iswind else 0.0,
                   'wspd_prev':0.5 if iswind and isdispatch else 0.0,                      
                   'wspd_next':0.5 if iswind and isdispatch else 0.0,  
                   'price':0.75 if isdispatch else 0.0, 
                   'price_prev':0.375 if isdispatch else 0.0,                        
                   'price_next':0.375 if isdispatch else 0.0}

        divisions = {'dni':4 if iscsp else 1,
                    'dni_prev':2 if iscsp else 1,
                    'dni_next':2 if iscsp else 1,
                    'ghi':4 if ispv else 1,
                    'ghi_prev':2 if ispv and isdispatch else 1,
                    'ghi_next':2 if ispv and isdispatch else 1,
                    'tdry':2 if iscsp else 1,
                    'wspd_solar':1, 
                    'wspd':4 if iswind else 1,
                    'wspd_prev':2 if iswind and isdispatch else 1,                      
                    'wspd_next':2 if iswind and isdispatch else 1,   
                    'price':4 if isdispatch else 1, 
                    'price_prev':2 if isdispatch else 1,                        
                    'price_next':2 if isdispatch else 1}

        
        # Set calculation boundaries for classification metrics: 'fullday = all hours, 'summer_daylight' = daylight hours at summer solstice
        bounds = {k:'fullday' for k in weights.keys()}   # 
        daylight_metrics = ['dni', 'dni_prev', 'dni_next', 'ghi', 'ghi_prev', 'ghi_next', 'wspd_solar']
        bounds.update({k:'summer_daylight' for k in daylight_metrics})

        return weights, divisions, bounds

    def read_weather(self):
        weather = {k:[] for k in ['year', 'month', 'day', 'hour', 'ghi', 'dhi', 'dni', 'tdry', 'wspd']}

        # Get header info
        header = np.genfromtxt(self.solar_resource_file, delimiter = ',', dtype = 'str', skip_header = 0, max_rows = 2)
        i = np.where(header[0,:] == 'Latitude')[0][0]
        weather['lat' ] = float(header[1,i])
        i = np.where(header[0,:] == 'Longitude')[0][0]
        weather['lon'] = float(header[1,i])
        i = np.where(header[0,:] == 'Time Zone')[0][0]
        weather['tz'] = float(header[1,i])
        i = np.where(header[0,:] == 'Elevation')[0][0]
        weather['elev'] = float(header[1,i]) 

        # Read in weather data
        labels = {'year': ['Year'],
                'month': ['Month'],
                'day': ['Day'],
                'hour': ['Hour'],
                'ghi': ['GHI'],
                'dhi': ['DHI'],
                'dni': ['DNI'],
                'tdry': ['Tdry', 'Temperature'],
                'wspd': ['Wspd', 'Wind Speed']}

        header = np.genfromtxt(self.solar_resource_file, dtype=str, delimiter=',', max_rows=1, skip_header=2)
        data = np.genfromtxt(self.solar_resource_file, dtype=float, delimiter=',', skip_header=3)
        for k in labels.keys():
            found = False
            for j in labels[k]:
                if j in header:
                    found = True
                    c = header.tolist().index(j)
                    weather[k] = data[:, c]
            if not found:
                print('Failed to find data for ' + k + ' in weather file')

        return weather

    def get_sunrise_sunset(self, lat, lon, utc_offset, day_of_year, year):
        day_start = datetime.datetime(year = year, month = 1, day = 1) + datetime.timedelta(days = day_of_year)
        time_utc = (day_start + datetime.timedelta(hours = 12) - datetime.timedelta(hours = utc_offset)).replace(tzinfo = datetime.timezone.utc)
        sunrise_utc, sunset_utc = pysolar.util.get_sunrise_sunset(lat, lon, time_utc)
        sunrise = (sunrise_utc + datetime.timedelta(hours = utc_offset)).replace(tzinfo = None)
        sunset = (sunset_utc + datetime.timedelta(hours = utc_offset)).replace(tzinfo = None)
        sunrise_hr = (sunrise - day_start).total_seconds()/3600 
        sunset_hr = (sunset - day_start).total_seconds()/3600 
        return sunrise_hr, sunset_hr

    def limit_outliers(self, array, cutoff_iqr = 3.0, max_iqr = 3.5):
        """
        Limit extreme outliers in data array prior to calculation of metrics for clustering 
        Values further than cutoff_iqr x (interquartile range) from the 25th or 75th percentile of the data will be scaled between cuttoff_iqr and max_iqr
        """
        is_normalized = (array.mean()>0.99 and array.mean()<1.01)
        q1, q3 = np.percentile(array, [25, 75])
        iqr = q3-q1  # Inter-quartile range
        high = q3 + cutoff_iqr*iqr
        low = q1 - cutoff_iqr*iqr
        ymax = array.max()
        ymin = array.min()
        ymax_scaled = min([ymax, q3+max_iqr*iqr])
        ymin_scaled = max([ymin, q1-max_iqr*iqr])
        array[array>high] = high + (array[array>high]-high)/(ymax - high) * (ymax_scaled - high)
        array[array<low] = low - (low - array[array<low])/(low - ymin) * (low - ymin_scaled)
        if is_normalized:  
            array /= array.mean()
        return array

    def calculate_metrics(self, sfavail = None):
        """
        sfavail = solar field availability with same time step as weather file
        """
    
        # Set weighting factors, averaging divisions, and averaging boundaries
        weights, divisions, bounds = self.get_default_weights()
        self.bounds = bounds
        if self.use_default_weights or len(list(self.weights.keys())) == 0:
            self.weights = weights
            self.divisions = divisions
        else:
            missing = [k for k in weights.keys() if k not in self.weights.keys()]
            self.weights.update({k:0.0 for k in missing})  # Set weight = zero to any non-specified 
            self.divisions.update({k:1 for k in missing})

        # Set weather data and wind speed data
        weather = self.read_weather()
        hourly_data = {k:weather[k] for k in ['dni', 'ghi', 'tdry']} 
        hourly_data['wspd_solar'] = weather['wspd']
        n_pts = len(hourly_data['ghi'])
        n_pts_day = int(n_pts / 365)
        n_per_hour = int(n_pts/8760)

        if self.wind_resource:
            hourly_data['wspd'] = self.wind_resource
        else:
            hourly_data['wspd'] = hourly_data['wspd_solar']
            if 'wind' in self.power_sources:
                print ('Warning: Wind speed data for wind generation was not supplied to clustering algorithm. Using wind speed from solar resource file')
        
        self.daily_avg_dni = np.zeros(365)
        for d in range(365):
            self.daily_avg_dni = hourly_data['dni'][d*n_pts_day : (d+1)*n_pts_day].mean() / 1000.  # kWh/m2/day


        #--- Replace dni, ghi or wind speed at all points with wind speed > stow limit
        csp_stow_wspd = None
        if 'tower' in self.power_sources or 'trough' in self.power_sources:
            csp_stow_wspd = max([self.wind_stow_limits[k] if k in self.power_sources else 0.0 for k in ['tower', 'trough']])
        pv_stow_wspd = self.wind_stow_limits['pv']
        wind_stow_wspd = self.wind_stow_limits['wind']

        if csp_stow_wspd:
            hourly_data['dni'][hourly_data['wspd_solar'] > csp_stow_wspd] = 0.0
            hourly_data['wspd_solar'][hourly_data['wspd_solar'] > csp_stow_wspd] = csp_stow_wspd

        if pv_stow_wspd:  # TODO: Should only apply for tracking PV, and GHI probably shouldn't be set to zero
            hourly_data['ghi'][hourly_data['wspd'] > pv_stow_wspd] = 0.0

        if wind_stow_wspd:
            hourly_data['wspd'][hourly_data['wspd'] > wind_stow_wspd] = wind_stow_wspd


        #--- Read in price data
        # TODO: Need to adjust price outliers before calculating classification metrics
        hourly_data['price'] = np.ones(n_pts)
        if self.price is None:
            if self.weights['price'] > 0 or self.weights['price_prev'] > 0 or self.weights['price_next'] > 0:
                print('Warning: Electricity price array was not provided. ' +
                    'Classification metrics will be calculated with a uniform price multiplier.')
        else:
            if len(self.price) == n_pts:
                hourly_data['price'] = np.array(self.price)
                if self.price_limit:
                    hourly_data['price'] = self.limit_outliers(hourly_data['price'], self.price_limit, self.price_limit+0.5)
            else:
                print('Warning: Specified price array and data in weather file have different lengths. ' +
                    'Classification metrics will be calculated with a uniform price multiplier')

        # TODO: REMOVED FOR NOW - May want to add this back for CSP
        # read in solar field availability data (could be adapted for wind farm or pv field availability)
        # hourly_data['sfavail'] = np.ones((n_pts))
        # if sfavail is None:
        #     if weights['avg_sfavail']>0:
        #         print('Warning: solar field availability was not provided.
        #         Weighting factor for average solar field availability will be reset to zero')
        #         weights['avg_sfavail'] = 0.0
        # else:
        #     if len(sfavail) == n_pts:
        #         hourly_data['sfavail'] = np.array(sfavail)
        #     else:
        #         print('Warning: Specified solar field availability array and data in weather file have different lengths.
        #         Weighting factor for average solar field availability will be reset to zero')
        #         weights['avg_sfavail'] = 0.0


        # Identify daylight hours on summer solstice
        sunrise, sunset = self.get_sunrise_sunset(weather['lat'], weather['lon'], weather['tz'], 172, int(weather['year'][0]))
        sunrise_idx = int(sunrise * n_per_hour)  
        sunset_idx = int(sunset * n_per_hour) + 1

        #--- Calculate daily values for classification metrics
        daily_metrics = {k:[] for k in self.weights.keys()}
        n_metrics = 0
        for key in self.weights.keys():
            if self.weights[key] > 0.0:  # Metric weighting factor is non-zero
                data_name = key.split('_')[0]
                n_div = self.divisions[key]  # Number of divisions per day
                daily_metrics[key] = np.zeros((365, n_div))
                if '_prev' in key or '_next' in key:
                    n_metrics += n_div  # TODO: should this *Nnext or *Nprev depending?? (This assumes 1 day for each)
                else:
                    n_metrics += n_div * self.ndays

                # Calculate average value in each division
                # (Averages with non-integer number of time points in a division are computed from weighted averages)
                n_pts = n_pts_day if bounds[key] == 'fullday' else sunset_idx - sunrise_idx  # Total points
                p1 = 0 if bounds[key] == 'fullday' else sunrise_idx    # First relevant point within this day
                n = float(n_pts) / n_div  # Number of time points per division
                pts = []
                wts = []
                for i in range(n_div):
                    pstart = i * n  # Start time pt
                    pend = (i + 1) * n  # End time pt
                    # Number of discrete hourly points included in the time period average
                    npt = int(pend) - int(pstart) + 1
                    # Discrete points which are at least partially included in the time period average
                    pts.append(np.linspace(int(pstart), int(pend), npt, dtype=int))
                    wts.append(1. / n * np.ones(npt))
                    wts[i][0] = float(1.0 - (pstart - int(pstart))) / n  # Weighting factor for first point
                    wts[i][npt - 1] = float(pend - int(pend)) / n  # Weighting factor for last point   

                # Calculate metrics for each day and each division
                for d in range(365):
                    for i in range(n_div):
                        for h in range(len(pts[i])):  # Loop over hours which are at least partially contained in division i
                            if pts[i][h] == n_pts:
                                # Hour falls outside of allowed number of hours in the day (allowed as long as weighting factor is 0)
                                if wts[i][h] > 0.0:
                                    print('Error calculating weighted average for key ' + key + ' and division ' + str(i))
                            else:
                                p = d * n_pts_day + p1 + pts[i][h]  # Point in yearly array
                                daily_metrics[key][d, i] += (hourly_data[data_name][p] * wts[i][h])

                # Normalize daily metrics
                max_metric = daily_metrics[key].max()
                min_metric = daily_metrics[key].min()
                daily_metrics[key] = (daily_metrics[key] - min_metric) / max(1e-6, max_metric - min_metric)


        

        #--- Create arrays of classification data for groups of days
        def get_data_for_group(d1, name):  # Get data for metric "name" for group starting on day d1
            if '_prev' in name:
                days = [d1-1]
            elif '_next' in name:
                days = [d1+self.ndays]
            else:
                days = [d1+i for i in range(self.ndays)]
            ndays = len(days)
            ndiv = self.divisions[name]
            data = np.zeros(ndiv*ndays)
            for j in range(ndays):
                d = days[j]
                if (d >= 0) and (d < 365):
                    data[j*ndiv:(j+1)*ndiv] = daily_metrics[name][d, :] * self.weights[name]
                else:
                    data[j*ndiv:(j+1)*ndiv] = -1e8  # Use a large neative value to designate metrics that don't exist for this group (all others are scaled between 0-1)
            return data          

        n_group = int(((365-2) / self.ndays))            # Number of complete groups (with existing days before/after)
        self.data = np.zeros((n_group, int(n_metrics)))  # Classification data for complete groups
        self.data_first, self.data_last = [np.zeros(n_metrics) for v in range(2)]  # Classification data for incomplete groups at beginning/end of year
        j = 0
        for k, wt in self.weights.items():
            if wt>0:
                for g in range(n_group):
                    d1 = g * self.ndays + 1
                    groupdata = get_data_for_group(d1, k)
                    n = len(groupdata)
                    self.data[g,j:j+n] = groupdata
                self.data_first[j:j+n] = get_data_for_group(0, k)
                self.data_last[j:j+n] = get_data_for_group(self.ndays*n_group+1, k)
                j+=n

        return 

    def create_clusters(self, verbose=False):
        # Create clusters from classification data.
        # Includes iterations of affinity propagation algorithm to create desired number of clusters if specified.
        if not self.afp_enforce_Ncluster:  # 
            self.form_clusters_using_current_parameters()
        else:
            maxiter = self.afp_enforce_Ncluster_maxiter
            Ntarget = self.n_cluster
            tol = self.afp_enforce_Ncluster_tol
            urf = 0.85

            mult = 1.0
            mult_prev = 1.0
            Nc_prev = 0
            i = 0
            finished = False
            damping_original = self.afp_damping

            while i < maxiter and not finished:
                self.afp_preference_mult = mult
                self.form_clusters_using_current_parameters()
                converged = self.clusters['converged']  # Did affinity propagation algorithm converge?  

                if verbose:
                    print('Formed %d clusters with preference multiplier %f' % (self.clusters['n_cluster'], mult))
                    if not converged:
                        print('Affinity propagation algorithm did not converge within the maximum allowable iterations')

                # Don't use this solution to create next guess for preference multiplier
                #   -> increase maximum iterations and damping and try again
                if not converged:
                    self.afp_damping += 0.05
                    self.afp_damping = min(self.afp_damping, 0.95)
                    if verbose:
                        print('Damping factor increased to %f' % (self.afp_damping))

                else:
                    # Algorithm converged -> use this solution and revert back to original damping and maximum number of iterations for next guess
                    self.afp_damping = damping_original
                    Nc = self.clusters['n_cluster']
                    if abs(self.clusters['n_cluster'] - Ntarget) <= tol:
                        finished = True
                    else:

                        if Nc_prev == 0 or Nc == Nc_prev:
                            # First successful iteration, or no change in clusters with change in preference multiplier
                            mult_new = mult * float(self.clusters['n_cluster']) / Ntarget
                        else:
                            dNcdmult = float(Nc - Nc_prev) / float(mult - mult_prev)
                            mult_new = mult - urf * float(Nc - Ntarget) / dNcdmult

                        if mult_new <= 0:
                            mult_new = mult * float(self.clusters['n_cluster']) / Ntarget

                        mult_prev = mult
                        Nc_prev = Nc
                        mult = mult_new

                i += 1

            if not finished:
                print('Maximum number of iterations reached without finding %d clusters. '
                    'The current number of clusters is %d' % (Ntarget, self.clusters['n_cluster']))


        if verbose:
            print('    Created %d clusters. WCSS = %.2f' % (self.clusters['n_cluster'], self.clusters['wcss']))

        # Sort clusters in order of lowest to highest exemplar points
        n_group = self.data.shape[0]  # Number of data points
        n_cluster = self.clusters['n_cluster']
        inds = self.clusters['exemplars'].argsort()
        clusters_sorted = {}
        for key in self.clusters.keys():
            if key in ['n_cluster', 'wcss']:
                clusters_sorted[key] = self.clusters[key]
            else:
                clusters_sorted[key] = np.empty_like(self.clusters[key])
        for i in range(n_cluster):
            k = inds[i]
            clusters_sorted['partition_matrix'][:, i] = self.clusters['partition_matrix'][:, k]
            for key in ['count', 'weights', 'exemplars']:
                clusters_sorted[key][i] = self.clusters[key][k]
            for key in ['means']:
                clusters_sorted[key][i, :] = self.clusters[key][k, :]
        for g in range(n_group):
            k = self.clusters['index'][g]
            clusters_sorted['index'][g] = inds.argsort()[k]
        
        self.clusters = clusters_sorted
        return 

    def form_clusters_using_current_parameters(self):
        # Create clusters from classification data using currently specified input parameters
        clusters = {}
        data = self.data
        n_group = data.shape[0]

        if n_group == 1:
            clusters['n_cluster'] = 1
            clusters['wcss'] = 0.0
            clusters['index'] = np.zeros(n_group, int)
            clusters['count'] = np.ones(1, int)
            clusters['means'] = np.ones((1, data.shape[1])) * data
            clusters['partition_matrix'] = np.ones((1, 1))
            clusters['exemplars'] = np.zeros(1, int)
            return clusters

        if self.afp_preference_mult == 1.0:  # Run with default preference
            pref = None
        else:
            distsqr = np.zeros((n_group,n_group))
            for g in range(n_group):
                distsqr[g,:] =  -((data[g,:] - data[:,:])**2).sum(1)  
            pref = (np.median(distsqr)) * self.afp_preference_mult

        alg = AffinityPropagation(damping = self.afp_damping, max_iter=self.Nmaxiter, convergence_iter=self.afp_Nconverge, preference=pref)
        alg.fit_predict(data)
        clusters['index'] = alg.cluster_index
        clusters['n_cluster'] = alg.n_clusters
        clusters['means'] = alg.cluster_means
        clusters['wcss'] = alg.wcss
        clusters['exemplars'] = alg.exemplars
        clusters['converged'] = alg.converged

        n_cluster = clusters['n_cluster']
        clusters['count'] = np.zeros(n_cluster, int)  # Number of data points nominally assigned to each Cluster
        clusters['partition_matrix'] = np.zeros((n_group, n_cluster))

        for k in range(n_cluster):
            clusters['count'][k] = np.sum(clusters['index'] == k)

        if self.sim_hard_partitions:
            inds = np.arange(n_group)
            clusters['partition_matrix'][inds, clusters['index'][inds]] = 1.0

        else:  # Compute "fuzzy" partition matrix
            distsqr = np.zeros((n_group, n_cluster))
            for k in range(n_cluster):
                distsqr[:, k] = ((data - clusters['means'][k, :]) ** 2).sum(1)  # Squared distance between all data points and Cluster mean k
            distsqr[distsqr == 0] = 1.e-10
            sumval = (distsqr ** (-2. / (self.mfuzzy - 1))).sum(1)  # Sum of dik^(-2/m-1) over all clusters k
            for k in range(n_cluster):
                clusters['partition_matrix'][:, k] = (distsqr[:, k] ** (2. / (self.mfuzzy - 1)) * sumval) ** -1

        # Sum of wij over all data points (i) / n_group
        clusters['weights'] = clusters['partition_matrix'].sum(0) / n_group

        self.clusters = clusters

        return 

    def set_sim_days(self):
        self.sim_start_days = (1 + self.clusters['exemplars']*self.ndays).tolist() 
        return
    
    def adjust_weighting_for_incomplete_groups(self):
        """
        Adjust Cluster weighting to account for incomplete groups at beginning and end of the year
        (excluded from original clustering algorithm because these days cannot be used as exemplar points)
        """
        ngroup, nfeatures = self.data.shape
        n_clusters = self.clusters['n_cluster']
        dist_first = np.zeros(n_clusters)
        dist_last = np.zeros(n_clusters)
        for k in range(n_clusters): 
            for f in range(nfeatures):
                if self.data_first[f] > -1.e6:  # Data feature f is defined for first set
                    dist_first[k] += (self.data_first[f] - self.clusters['means'][k, f]) ** 2
                if self.data_last[f] > -1.e6:
                    dist_last[k] += (self.data_last[f] - self.clusters['means'][k, f]) ** 2

        self.index_first = dist_first.argmin()  # Cluster which best represents first days
        self.index_last = dist_last.argmin()    # Cluster which best represents last days

        # Recompute Cluster weights
        nfirst = 1.
        nlast = 365 - ngroup*self.ndays - 1  # Number of days in incomplete last group
        ngroup_adj = ngroup + (nfirst/self.ndays) + (nlast/self.ndays)  # Adjusted total number of groups
        s = self.clusters['partition_matrix'].sum(0)
        s[self.index_first] = s[self.index_first] + (nfirst/self.ndays)  # Apply fraction of first 
        s[self.index_last] = s[self.index_last] + (nlast/self.ndays)
        self.clusters['weights_adjusted'] = s / ngroup_adj

        return  

    def run_clustering(self, verbose = False):
        self.calculate_metrics() 
        self.create_clusters(verbose)
        self.set_sim_days()
        self.adjust_weighting_for_incomplete_groups()  
        return

    def get_sim_start_end_times(self, clusterid: int):
        # Times (seconds) to start and end simulation for designated cluster
        d = self.sim_start_days[clusterid]
        time_start = (d-1)*24*3600
        time_end = (d+self.ndays+1)*24*3600
        return time_start, time_end

    def get_soln_start_end_times(self, clusterid: int):
        # Times (seconds) to save solution values for designated cluster
        d = self.sim_start_days[clusterid]
        time_start = d*24*3600
        time_end = (d+self.ndays)*24*3600
        return time_start, time_end

    def csp_soc_heuristic(self, clusterid: int, solar_multiple = None):
        '''
        Returns initial TES hot charge state (%) at the beginning of the first simulated day in a cluster
        Note that an extra full day is simulated at the beginning of each exemplar. The SOC specified here only needs to provide a reasonable SOC after one day of simulation
        '''
        d = self.sim_start_days[clusterid]
        prev_day_dni = self.daily_avg_dni[max(0, d-2)]  # Daily average DNI during day prior to first simulated day
  
        if not solar_multiple:
            initial_soc = 10
        else:
            if prev_day_dni < 6.0 or solar_multiple < 1.5:  # Low solar multiple or poor previous-day DNI
                initial_soc = 5
            else:
                initial_soc = 10 if solar_multiple < 2.0 else 20
        return initial_soc
        

    def battery_soc_heuristic(self, clusterid: int):
        '''
        Returns initial battery SOC at the beginning of the first simulated day in a cluster
        Note that an extra full day is simulated at the beginning of each exemplar. The SOC specified here only needs to provide a reasonable SOC after one day of simulation
        '''
        return 0

    def compute_annual_array_from_cluster_exemplar_data(self, exemplardata, dtype=float):
        """
        # Create full year hourly array from hourly array containing only data at exemplar points (Note, data can exist outside of exemplar points, but will not be used)
        exemplardata = full-year hourly array with data existing only at days within exemplar groupings
        adjust_wt = adjust calculations with first/last days allocated to a Cluster
        """
        npts = len(exemplardata)  # Total number of points in a year
        fulldata = np.zeros((npts))
        ngroup, ncluster = self.clusters['partition_matrix'].shape
        nptshr = int(npts / 8760)
        nptsday = nptshr * 24

        data = np.zeros((nptsday * self.ndays, ncluster))  # Hourly data for each Cluster exemplar
        for k in range(ncluster):
            d = self.sim_start_days[k]   # Starting days for each exemplar grouping
            data[:, k] = exemplardata[d * nptsday:(d + self.ndays) * nptsday]

        for g in range(ngroup):
            d = g * self.ndays + 1  # Starting day for data group g
            fulldata[d * nptsday:(d + self.ndays) * nptsday] = (self.clusters['partition_matrix'][g, :] * data).sum(1)  # Sum of partition matrix x exemplar data points for each hour

        # Fill in first/last days 
        k1 = self.index_first
        k2 = self.index_last
        if k1 >= 0 and k2 >= 0:
            d = self.sim_start_days[k1]    # Starting day for group to which day 0 is assigned
            fulldata[0:nptsday] = fulldata[d * nptsday:(d + 1) * nptsday]
            d = self.sim_start_days[k2]  # Starting day for group to which incomplete last group is assigned
            dstart = ngroup*self.ndays+1 # Starting day for incomplete last group
            fulldata[dstart * nptsday:(dstart + self.ndays) * nptsday] = fulldata[d * nptsday:(d + self.ndays) * nptsday]
        else:  # TODO: Is this needed anymore?  Calculations should be generalized to >2-day clusters, so probably can remove?  
            navg = 5
            if max(fulldata[0:24]) == 0:  # No data for first day of year
                print(
                    'First day of the year was not assigned to a Cluster and will be assigned average generation profile from the next ' + str(
                        navg) + ' days.')
                hourly_avg = np.zeros((nptsday))
                for d in range(1, navg + 1):
                    for h in range(24 * nptshr):
                        hourly_avg[h] += fulldata[d * nptsday + h] / navg
                fulldata[0:nptsday] = hourly_avg

            nexclude = 364 - ngroup * self.ndays # Number of excluded days at the end of the year
            if nexclude > 0:
                h1 = 8760 * nptshr - nexclude * nptsday  # First excluded hour at the end of the year
                if max(fulldata[h1: h1 + nexclude * nptsday]) == 0:
                    print('Last ' + str(
                        nexclude) + ' days were not assigned to a Cluster and will be assigned average generation profile from prior ' + str(
                        navg) + ' days.')
                    hourly_avg = np.zeros((nexclude * nptsday))
                    d1 = 365 - nexclude - navg  # First day to include in average
                    for d in range(d1, d1 + navg):
                        for h in range(nptsday):
                            hourly_avg[h] += fulldata[d * nptsday + h] / navg
                    fulldata[h1: h1 + nexclude * nptsday] = hourly_avg

        if dtype is bool:
            fulldata = np.array(fulldata, dtype=bool)

        return fulldata.tolist()


class AffinityPropagation:
    # Affinity propagation algorithm

    def __init__(self, damping=0.5, max_iter=300, convergence_iter=10, preference=None):
        self.damping = damping  # Damping factor for update of responsibility and availability matrices (0.5 - 1)
        self.max_iter = max_iter  # Maximum number of iterations
        # Number of iterations without change in clusters or exemplars to define convergence
        self.convergence_iter = convergence_iter
        # Preference for all data points to serve as exemplar.
        #   If None, the preference will be set to the median of the input similarities
        self.preference = preference
        self.random_seed = 123

        # This attributes are filled by fit_predict()
        self.n_clusters = None
        self.cluster_means = None
        self.cluster_index = None
        self.wcss = None
        self.exemplars = None
        self.converged = None

    def compute_wcss(self, data, cluster_index, means):
        # Computes the within-cluster sum-of-squares
        n_clusters = means.shape[0]
        self.wcss = 0.0
        for k in range(n_clusters):
            dist = ((data - means[k, :]) ** 2).sum(1)  # Distance to Cluster k centroid
            self.wcss += (dist * (cluster_index == k)).sum()

    def fit_predict(self, data):
        n_obs, n_features = data.shape  # Number of observations and features

        # Compute similarities between data points (negative of Euclidean distance)
        S = np.zeros((n_obs, n_obs))
        inds = np.arange(n_obs)
        for p in range(n_obs):
            # Negative squared Euclidean distance between pt p and all other points
            S[p, :] = -((data[p, :] - data[:, :]) ** 2).sum(1)

        if self.preference:  # Preference is specified
            S[inds, inds] = self.preference
        else:
            pref = np.median(S)
            S[inds, inds] = pref

        np.random.seed(self.random_seed)
        mag = abs(S).min()
        S += 1.e-8*mag * S * (np.random.random_sample((n_obs, n_obs)) - 0.5)

        # Initialize availability and responsibility matrices
        A = np.zeros((n_obs, n_obs))
        R = np.zeros((n_obs, n_obs))
        exemplars = np.zeros(n_obs, bool)

        q = 0
        count = 0
        while (q < self.max_iter) and (count < self.convergence_iter):
            exemplars_prev = exemplars
            update = np.zeros((n_obs, n_obs))

            # Update responsibility
            M = A + S
            k = M.argmax(axis=1)  # Location of maximum value in each row of M
            maxval = M[inds, k]  # Maximum values in each row of M
            update = S - np.reshape(maxval, (n_obs, 1))  # S - max value in each row
            M[inds, k] = -np.inf
            k2 = M.argmax(axis=1)  # Location of second highest value in each row of M
            maxval = M[inds, k2]  # Second highest value in each row of M
            update[inds, k] = S[inds, k] - maxval
            R = self.damping * R + (1. - self.damping) * update

            # Update availability
            posR = R.copy()
            posR[posR < 0] = 0.0  # Only positive values of R matrix
            sumR = posR.sum(0)
            values = sumR - np.diag(posR)
            update[:] = values  # Sum positive values of R over all rows (i)
            update -= posR
            update[:, inds] += np.diag(R)
            update[update > 0] = 0.0
            update[inds, inds] = values
            A = self.damping * A + (1. - self.damping) * update

            # Identify exemplars
            exemplars = (np.diag(A) + np.diag(R)) > 0
            #exemplars = (A + R).argmax(1) == inds  # Exemplar for point i is value of k that maximizes A[i,k]+R[i,k]
            diff = (exemplars != exemplars_prev).sum()
            if diff == 0:
                count += 1
            else:
                count = 0
            q += 1

        if count < self.convergence_iter and q >= self.max_iter:
            converged = False
        else:
            converged = True

        exemplars = np.where(exemplars == True)[0]
        found_exemplars = exemplars.shape[0]

        # Modify final set of clusters to ensure that the chosen exemplars minimize wcss
        S[inds, inds] = 0.0  # Replace diagonal entries in S with 0
        S[:, :] = -S[:, :]  # Revert back to actual distance
        clusters = S[:, exemplars].argmin(1)  # Assign points to clusters based on distance to the possible exemplars
        for k in range(found_exemplars):  # Loop over clusters
            pts = np.where(clusters == k)[0]  # All points in Cluster k
            n_pts = len(pts)
            if n_pts > 2:
                dist_sum = np.zeros(n_pts)
                for p in range(n_pts):
                    # Calculate total distance between point p and all other points in Cluster k
                    dist_sum[p] += (S[pts[p], pts]).sum()
                i = dist_sum.argmin()
                exemplars[k] = pts[i]  # Replace exemplar k with point that minimizes wcss

        # Assign points to clusters based on distance to the possible exemplars
        clusters = S[:, exemplars].argmin(1)
        cluster_means = data[exemplars, :]  
        cluster_index = clusters
        self.compute_wcss(data, cluster_index, cluster_means)

        self.n_clusters = found_exemplars
        self.cluster_means = cluster_means
        self.cluster_index = cluster_index
        self.exemplars = exemplars
        self.converged = converged

        return self









# TODO: probably don't need these anymore, delete?  
'''
def compute_cluster_avg_from_timeseries(hourly, partition_matrix, Ndays, Nprev=1, Nnext=1, adjust_wt=False, k1=None,
                                        k2=None):
    """
    # Compute Cluster-average hourly values from full-year hourly array and partition matrix

    hourly = full annual array of data 
    partition_matrix = partition matrix from clustering (rows = data points, columns = clusters)
    Ndays = number of simulated days (not including previous/next)
    Nprev = number of previous days that will be included in the simulation
    Nnext = number of subsequent days that will be included in the simulation
    adjust_wt = adjust calculations with first/last days allocated to a Cluster
    k1 = Cluster to which first day belongs
    k2 = Cluster to which last day belongs
    
    ouput = list of Cluster-average hourly arrays for the (Nprev+Ndays+Nnext) days simulated within the Cluster
    """
    Ngroup, Ncluster = partition_matrix.shape
    Ndaystot = Ndays + Nprev + Nnext  # Number of days that will be included in the simulation (including previous / next days)
    Nptshr = int(len(hourly) / 8760)

    avg = np.zeros((Ncluster, Ndaystot * 24 * Nptshr))
    for g in range(Ngroup):
        d = g * Ndays + 1  # First day to be counted in simulation group g
        d1 = max(0, d - Nprev)  # First day to be included in simulation group g (Nprev days before day d if possible)
        Nprev_actual = d - d1  # Actual number of previous days that can be included
        Ndaystot_actual = Ndays + Nprev_actual + Nnext
        h = d1 * 24 * Nptshr  # First time point included in simulation group g
        if Nprev == Nprev_actual:
            vals = np.array(hourly[
                            h:h + Ndaystot * 24 * Nptshr])  # Hourly values for only the days included in the simulation for group g
        else:  # Number of previous days was reduced (only occurs at beginning of the year)
            Nvoid = Nprev - Nprev_actual  # Number of previous days which don't exist in the data file (only occurs when Nprev >1)
            vals = []
            for v in range(Nvoid):  # Days for which data doesn't exist
                vals = np.append(vals, hourly[0:24 * Nptshr])  # Use data from first day
            vals = np.append(vals, hourly[h:h + Ndaystot_actual * 24 * Nptshr])

        for k in range(Ncluster):
            avg[k, :] += vals * partition_matrix[
                g, k]  # Sum of hourly array * partition_matrix value for Cluster k over all points (g)

    for k in range(Ncluster):
        avg[k, :] = avg[k, :] / partition_matrix.sum(0)[
            k]  # Divide by sum of partition matrix over all groups to normalize

    if adjust_wt and Ndays == 2:  # Adjust averages to include first/last days of the year
        avgnew = avg[k1, Nprev * 24 * Nptshr:(Nprev + 1) * 24 * Nptshr] * partition_matrix.sum(0)[
            k1]  # Revert back to non-normalized values for first simulation day in which results will be counted
        avgnew += hourly[0:24 * Nptshr]  # Update values to include first day
        avg[k1, Nprev * 24 * Nptshr:(Nprev + 1) * 24 * Nptshr] = avgnew / (partition_matrix.sum(0)[
                                                                               k1] + 1)  # Normalize values for first day and insert back into average array

        avgnew = avg[k2, 0:(Ndays + Nprev) * 24 * Nptshr] * partition_matrix.sum(0)[
            k2]  # Revert back to non-normalized values for the previous day and two simulated days
        avgnew += hourly[
                  (363 - Nprev) * 24 * Nptshr:365 * 24 * Nptshr]  # Update values to include the last days of the year
        avg[k2, 0:(Ndays + Nprev) * 24 * Nptshr] = avgnew / (
                    partition_matrix.sum(0)[k2] + 1)  # Normalize values and insert back into average array

    return avg.tolist()


def setup_clusters(weather_file, ppamult, n_clusters, Ndays=2, Nprev=1, Nnext=1, user_weights=None, user_divisions=None):
    # Clustering inputs that have no dependence on independent variables

    algorithm = 'affinity-propagation'
    hard_partitions = True
    afp_enforce_Ncluster = True

    # Calculate classification metrics
    ret = calc_metrics(weather_file=weather_file, Ndays=Ndays, ppa=ppamult, user_weights=user_weights,
                       user_divisions=user_divisions, stow_limit=None)
    data = ret['data']
    data_first = ret['firstday']
    data_last = ret['lastday']

    # Create clusters
    cluster_ins = Cluster()
    cluster_ins.algorithm = algorithm
    cluster_ins.n_cluster = n_clusters
    cluster_ins.sim_hard_partitions = hard_partitions
    cluster_ins.afp_enforce_Ncluster = afp_enforce_Ncluster
    clusters = create_clusters(data, cluster_ins)
    sim_start_days = (1 + clusters['exemplars'] * Ndays).tolist()

    # Adjust weighting for first and last days
    ret = adjust_weighting_firstlast(data, data_first, data_last, clusters, Ndays)
    clusters = ret[0]
    firstpt_cluster = ret[1]
    lastpt_cluster = ret[2]

    # Calculate Cluster-average PPA multipliers and solar field adjustment factors
    avg_ppamult = compute_cluster_avg_from_timeseries(ppamult, clusters['partition_matrix'], Ndays=Ndays, Nprev=Nprev,
                                                      Nnext=Nnext, adjust_wt=True, k1=firstpt_cluster,
                                                      k2=lastpt_cluster)

    cluster_inputs = {}
    cluster_inputs['exemplars'] = clusters['exemplars']
    cluster_inputs['weights'] = clusters['weights_adjusted']
    cluster_inputs['day_start'] = sim_start_days
    cluster_inputs['partition_matrix'] = clusters['partition_matrix']
    cluster_inputs['first_pt_cluster'] = firstpt_cluster
    cluster_inputs['last_pt_cluster'] = lastpt_cluster
    cluster_inputs['avg_ppamult'] = avg_ppamult

    return cluster_inputs


def create_annual_array_with_cluster_average_values(hourly, cluster_average, start_days, Nsim_days, Nprev=1, Nnext=1,
                                                    overwrite_surrounding_days=False):
    """
    # Create full year array of hourly data with sections corresponding to Cluster exemplar simulations overwritten
    with Cluster-average values

    hourly = full year of hourly input data
    cluster_average = groups of Cluster-average input data
    start_days = list of Cluster start days
    Nsim_days = list of number of days simulated within each Cluster
    Nprev = number of previous days included in the simulation
    Nnext = number of subsequent days included in teh simulation
    """
    Ng = len(start_days)
    output = hourly
    Nptshr = int(len(hourly) / 8760)
    Nptsday = Nptshr * 24
    count_days = []
    for g in range(Ng):
        for d in range(Nsim_days[g]):
            count_days.append(start_days[g] + d)

    for g in range(Ng):  # Number of simulation groupings
        Nday = Nsim_days[g]  # Number of days counted in simulation group g
        Nsim = Nsim_days[g] + Nprev + Nnext  # Number of simulated days in group g
        for d in range(Nsim):  # Days included in simulation for group g
            day_of_year = (start_days[g] - Nprev) + d
            if d >= Nprev and d < Nprev + Nday:  # Days that will be counted in results
                for h in range(Nptsday):
                    output[day_of_year * Nptsday + h] = cluster_average[g][d * Nptsday + h]

            else:  # Days that will not be counted in results
                if overwrite_surrounding_days:
                    if day_of_year not in count_days and day_of_year >= 0 and day_of_year < 365:
                        for h in range(Nptsday):
                            output[day_of_year * Nptsday + h] = cluster_average[g][d * Nptsday + h]

    return output


def compute_annual_array_from_clusters(exemplardata, clusters, Ndays, adjust_wt=False, k1=None, k2=None, dtype=float):
    """
    # Create full year hourly array from hourly array containing only data at exemplar points

    exemplardata = full-year hourly array with data existing only at days within exemplar groupings
    clusters = Cluster information
    Ndays = number of consecutive simulation days within each group
    adjust_wt = adjust calculations with first/last days allocated to a Cluster
    k1 = Cluster to which first day belongs
    k2 = Cluster to which last day belongs
    """
    npts = len(exemplardata)
    fulldata = np.zeros((npts))
    ngroup, ncluster = clusters['partition_matrix'].shape
    nptshr = int(npts / 8760)
    nptsday = nptshr * 24

    data = np.zeros((nptsday * Ndays, ncluster))  # Hourly data for each Cluster exemplar
    for k in range(ncluster):
        d = clusters['exemplars'][k] * Ndays + 1  # Starting days for each exemplar grouping
        data[:, k] = exemplardata[d * nptsday:(d + Ndays) * nptsday]

    for g in range(ngroup):
        d = g * Ndays + 1  # Starting day for data group g
        avg = (clusters['partition_matrix'][g, :] * data).sum(
            1)  # Sum of partition matrix x exemplar data points for each hour
        fulldata[d * nptsday:(d + Ndays) * nptsday] = avg

    # Fill in first/last days 
    if adjust_wt and k1 >= 0 and k2 >= 0 and Ndays == 2:
        d = (clusters['exemplars'][k1]) * Ndays + 1  # Starting day for group to which day 0 is assigned
        fulldata[0:nptsday] = fulldata[d * nptsday:(d + 1) * nptsday]
        d = (clusters['exemplars'][k2]) * Ndays + 1  # Starting day for group to which days 363 and 364 are assigned
        fulldata[363 * nptsday:(363 + Ndays) * nptsday] = fulldata[d * nptsday:(d + Ndays) * nptsday]
    else:
        navg = 5
        if max(fulldata[0:24]) == 0:  # No data for first day of year
            print(
                'First day of the year was not assigned to a Cluster and will be assigned average generation profile from the next ' + str(
                    navg) + ' days.')
            hourly_avg = np.zeros((nptsday))
            for d in range(1, navg + 1):
                for h in range(24 * nptshr):
                    hourly_avg[h] += fulldata[d * nptsday + h] / navg
            fulldata[0:nptsday] = hourly_avg

        nexclude = 364 - ngroup * Ndays  # Number of excluded days at the end of the year
        if nexclude > 0:
            h1 = 8760 * nptshr - nexclude * nptsday  # First excluded hour at the end of the year
            if max(fulldata[h1: h1 + nexclude * nptsday]) == 0:
                print('Last ' + str(
                    nexclude) + ' days were not assigned to a Cluster and will be assigned average generation profile from prior ' + str(
                    navg) + ' days.')
                hourly_avg = np.zeros((nexclude * nptsday))
                d1 = 365 - nexclude - navg  # First day to include in average
                for d in range(d1, d1 + navg):
                    for h in range(nptsday):
                        hourly_avg[h] += fulldata[d * nptsday + h] / navg
                fulldata[h1: h1 + nexclude * nptsday] = hourly_avg

    if dtype is bool:
        fulldata = np.array(fulldata, dtype=bool)

    return fulldata.tolist()


def combine_consecutive_exemplars(days, weights, avg_ppamult, avg_sfadjust, Ndays=2, Nprev=1, Nnext=1):
    """
    Combine consecutive exemplars into a single simulation

    days = starting days for simulations (not including previous days)
    weights = Cluster weights
    avg_ppamult = average hourly ppa multipliers for each Cluster (note: arrays include all previous and subsequent days)
    avg_sfadjust = average hourly solar field adjustment factors for each Cluster (note: arrays include all previous and subsequent days)
    Ndays = number of consecutive days for which results will be counted
    Nprev = number of previous days which are included before simulation days
    Nnext = number of subsequent days which are included after simulation days
    """

    Ncombine = sum(np.diff(
        days) == Ndays)  # Number of simulation groupings that can be combined (starting days represent consecutive groups)
    Nsim = len(days) - Ncombine  # Number of simulation grouping after combination
    Nptshr = int(len(avg_ppamult[0]) / ((Ndays + Nprev + Nnext) * 24))  # Number of points per hour in input arrays
    group_index = np.zeros((len(days)))
    start_days = np.zeros((Nsim), int)
    sim_days = np.zeros((Nsim), int)
    g = -1
    for i in range(len(days)):
        if i == 0 or days[i] - days[i - 1] != Ndays:  # Day i starts new simulation grouping
            g += 1
            start_days[g] = days[i]
        sim_days[g] += Ndays
        group_index[i] = g

    group_weight = []
    group_avgppa = []
    group_avgsfadj = []
    h1 = Nprev * 24 * Nptshr  # First hour of "simulation" day in any Cluster
    h2 = (Ndays + Nprev) * 24 * Nptshr  # Last hour of "simulation" days in any Cluster
    hend = (Ndays + Nprev + Nnext) * 24 * Nptshr  # Last hour of "next" day in any Cluster
    for i in range(len(days)):
        g = group_index[i]
        if i == 0 or g != group_index[i - 1]:  # Start of new group
            wt = [float(weights[i])]
            avgppa = avg_ppamult[i][0:h2]
            avgsfadj = avg_sfadjust[i][0:h2]
        else:  # Continuation of previous group
            wt.append(weights[i])
            avgppa = np.append(avgppa, avg_ppamult[i][h1:h2])
            avgsfadj = np.append(avgsfadj, avg_sfadjust[i][h1:h2])

        if i == len(days) - 1 or g != group_index[i + 1]:  # End of group
            avgppa = np.append(avgppa, avg_ppamult[i][h2:hend])
            avgsfadj = np.append(avgsfadj, avg_sfadjust[i][h2:hend])
            group_weight.append(wt)
            group_avgppa.append(avgppa.tolist())
            group_avgsfadj.append(avgsfadj.tolist())

    combined = {}
    combined['start_days'] = start_days.tolist()
    combined['Nsim_days'] = sim_days.tolist()
    combined['avg_ppa'] = group_avgppa
    combined['avg_sfadj'] = group_avgsfadj
    combined['weights'] = group_weight

    return combined
'''