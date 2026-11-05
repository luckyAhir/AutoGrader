#include <stdio.h>

/* Adds up all the numbers given on the input. */
int main(void)
{
    int value;
    long long total = 0;

    while (scanf("%d", &value) != 0) {
        total += value;
    }
    printf("%lld\n", total);
    return 0;
}
