/* Maximum subarray sum with Kadane's algorithm, O(n). */
#include <stdio.h>
#include <stdlib.h>

/* Largest sum of a non-empty contiguous run of values[0..count-1]. */
static long long max_subarray(const long long *values, int count)
{
    long long best = values[0];
    long long ending_here = values[0];
    for (int i = 1; i < count; i++) {
        long long extended = ending_here + values[i];
        ending_here = extended > values[i] ? extended : values[i];
        if (ending_here > best) {
            best = ending_here;
        }
    }
    return best;
}

int main(void)
{
    int count;
    if (scanf("%d", &count) != 1 || count < 1) {
        return 1;
    }
    long long *values = malloc((size_t)count * sizeof *values);
    if (values == NULL) {
        return 1;
    }
    for (int i = 0; i < count; i++) {
        if (scanf("%lld", &values[i]) != 1) {
            free(values);
            return 1;
        }
    }
    printf("%lld\n", max_subarray(values, count));
    free(values);
    return 0;
}
